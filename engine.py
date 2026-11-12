"""
מנוע חישוב גמול השתלמות - עוז לתמורה
=======================================
שכבות:
1. DB (PostgreSQL) - שומר את "ספר החוקים" (מטא-דאטה: מקור, סעיף, תוקף, סדר הרצה),
   נתוני מורים, קורסים, וסטטוס תעסוקתי שנתי.
2. rules_registry (בפייתון) - הלוגיקה בפועל של כל חוק, מקושרת ל-rule_id
   שמופיע בטבלת rules ב-DB. זו ההפרדה בין "מה החוק אומר" (DB, קריא לאדם)
   לבין "איך הוא מתבצע" (קוד, ניתן להרצה).
3. Engine - מריץ את החוקים לפי סדר עדיפות על קלט מורה נתון, ומפיק
   פלט + trace מלא (audit trail) שמסביר איזה חוק תרם מה לתוצאה הסופית.
"""

import json
import math
import os
import secrets
from datetime import date, datetime
from dataclasses import dataclass, field

from db import get_connection
from rule_vocab import seed_rule_logic

# ----------------------------------------------------------------
# מיפוי שנות לימודים עבריות לתאריך תחילה משוער (1 בספטמבר)
# ----------------------------------------------------------------
SCHOOL_YEAR_START = {
    'תשע"ב': "2011-09-01", 'תשע"ג': "2012-09-01", 'תשע"ד': "2013-09-01",
    'תשע"ה': "2014-09-01", 'תשע"ו': "2015-09-01", 'תשע"ז': "2016-09-01",
    'תשע"ח': "2017-09-01", 'תשע"ט': "2018-09-01", 'תש"פ': "2019-09-01",
    'תשפ"א': "2020-09-01", 'תשפ"ב': "2021-09-01", 'תשפ"ג': "2022-09-01",
    'תשפ"ד': "2023-09-01", 'תשפ"ה': "2024-09-01", 'תשפ"ו': "2025-09-01",
}


def school_year_of(d: str) -> str:
    """מחזיר את שנת הלימודים העברית המקבילה לתאריך ISO נתון (קירוב לפי 1.9)."""
    dt = datetime.fromisoformat(d).date()
    years = sorted(SCHOOL_YEAR_START.items(), key=lambda x: x[1])
    result = years[0][0]
    for name, start in years:
        if dt >= datetime.fromisoformat(start).date():
            result = name
        else:
            break
    return result


# ==================================================================
# שלב 1: יצירת ה-DB וזריעת טבלת החוקים (המטא-דאטה)
# ==================================================================

RULES_METADATA = [
    dict(rule_id="R01_UNIT_HOURS", topic="גמול_השתלמות", title="יחידת גמול = שעות לימוד",
         description="יחידת גמול השתלמות אחת = 112 שעות לימוד; אם עם ציון, לפחות 56 מהן בציון 60+.",
         source_doc="חוזר_מנכל_השתלמויות", source_ref="3.4.2", priority=10, applies_when="תמיד"),
    dict(rule_id="R01B_GRADED_COMPOSITION", topic="גמול_השתלמות", title="הרכב שעות עם ציון ליחידה",
         description="מתוך שעות היחידה (112, או 168 לגמלאי), לפחות מחצית (56/84 בהתאמה) חייבות להיות שעות "
                     "עם ציון 60 ומעלה. שעות עם ציון ללא ציון רשום נחשבות עומדות בתנאי (בהנחת מעבר, כי ייבוא "
                     "מה-PDF לא מדווח ציון מספרי); שעות עם ציון רשום מתחת ל-60 אינן נחשבות עומדות בתנאי, אך "
                     "עדיין נספרות בסך השעות הכולל.",
         source_doc="חוזר_מנכל_השתלמויות", source_ref="3.4.2 (חלק מתיאור R01 - טרם אומת מול המקור המלא, ראו פערים ידועים)",
         priority=10, applies_when="תמיד"),
    dict(rule_id="R15_PENSIONER_RATE", topic="גמול_השתלמות", title="יחידת גמול לגמלאי = 168 שעות",
         description="עבור מורה גמלאי/העומד לפרוש, יחידת גמול = 168 שעות (במקום 112).",
         source_doc="חוזר_מנכל_השתלמויות", source_ref="6.9.2", priority=11,
         applies_when="is_pensioner = 1", overrides="R01_UNIT_HOURS"),
    dict(rule_id="R02_BASE_CAP", topic="גמול_השתלמות", title="תקרה כללית - 16 יחידות",
         description="תקרת יחידות גמול רגילות (ללא כפל תואר/ביניים) = 16, לפני עדכון עוז לתמורה.",
         source_doc="חוזר_מנכל_השתלמויות", source_ref="3.4.2", priority=20, applies_when="in_reform = 0"),
    dict(rule_id="R03_REFORM_CAP", topic="גמול_השתלמות", title="עדכון תקרה - עוז לתמורה",
         description="למורה בתנאי עוז לתמורה: תקרה=18 עד תש\"פ, ו-19 מתש\"פ (1.9.2019) ואילך.",
         source_doc="הסכם_עוז_לתמורה", source_ref="סעיף 18(א) + נוהל תש\"פ", priority=21,
         applies_when="in_reform = 1", overrides="R02_BASE_CAP"),
    dict(rule_id="R04_DOUBLE_DEGREE", topic="גמול_השתלמות", title="גמול כפל תואר",
         description="8.75 יחידות, פעם אחת בקריירה, לא נספר בתוך התקרה הרגילה.",
         source_doc="חוזר_מנכל_השתלמויות", source_ref="3.3.2", priority=30, applies_when="has_double_degree_gimul flag"),
    dict(rule_id="R05_BEINAYIM", topic="גמול_השתלמות", title="גמול ביניים",
         description="4 יחידות, פעם אחת, לא ניתן במקביל לכפל תואר.",
         source_doc="חוזר_מנכל_השתלמויות", source_ref="3.4.1", priority=31, applies_when="has_beinayim_gimul flag"),
    dict(rule_id="R04B_ADDITIONAL_DOUBLE_DEGREE", topic="גמול_השתלמות", title="כפל תואר נוסף (\"כפל שני\")",
         description="למורה שכבר קיבל/ה כפל תואר (R04) והשלים/ה לימודים נוספים המזכים בכך - עוד 4 יחידות, "
                     "בנוסף ל-8.75 (לא במקומן). רק אם has_double_degree_gimul=1.",
         source_doc="חוזר_מנכל_השתלמויות", source_ref="3.3.3.2", priority=32,
         applies_when="has_additional_double_degree_gimul flag AND has_double_degree_gimul flag"),
    dict(rule_id="R06_TOTAL_MAX", topic="גמול_השתלמות", title="תקרה כוללת 24.75",
         description="סך כל היחידות (רגילות + כפל תואר/ביניים) לא יעלה על 24.75. חל רק על מורים שאינם "
                     "בתנאי עוז לתמורה. למורה ברפורמה, יחידות כפל תואר/ביניים ניתנות בנוסף לתקרת הרפורמה "
                     "(18/19) וללא תקרה כוללת נוספת - סעיף 18(א): \"בנוסף ליחידות הגמול המוכרות בגין כפל "
                     "תואר, שאינן נספרות בתוך התקרה\".",
         source_doc="חוזר_מנכל_השתלמויות", source_ref="3.4.2", priority=90, applies_when="in_reform = 0",
         overrides=None),
    dict(rule_id="R06B_NO_CAP_REFORM", topic="גמול_השתלמות", title="אין תקרה כוללת - עוז לתמורה",
         description="למורה בתנאי עוז לתמורה, יחידות כפל תואר/כפל שני/ביניים נוספות במלואן על תקרת "
                     "הרפורמה (18/19), ללא הגבלת 24.75 - הן \"אינן נספרות בתוך התקרה\".",
         source_doc="הסכם_עוז_לתמורה", source_ref="סעיף 18(א)", priority=90,
         applies_when="in_reform = 1", overrides="R06_TOTAL_MAX"),
    dict(rule_id="R07_PCT_RATE", topic="גמול_השתלמות", title="שיעור תשלום ליחידה - 1.2%",
         description="כל יחידת גמול = 1.2% מהשכר המשולב. חל על מורים שאינם בתנאי עוז לתמורה, וכן על מורי "
                     "עוז לתמורה עד 1.9.2025.",
         source_doc="חוזר_מנכל_השתלמויות", source_ref="3.4.2 סוף", priority=91, applies_when="in_reform = 0 OR calc_date < 2025-09-01"),
    dict(rule_id="R07B_PCT_RATE_REFORM", topic="גמול_השתלמות", title="עדכון שיעור ליחידה ל-1.3% (חטיבות עליונות)",
         description="החל מ-1.9.2025 (תשפ\"ו) שיעור התשלום ליחידת גמול עלה מ-1.2% ל-1.3% למורי חטיבה "
                     "עליונה, בין אם בעוז לתמורה ובין אם לאו. באפיון הנוכחי כל מורי עוז לתמורה מוגדרים "
                     "כמלמדים בחטיבה עליונה (in_reform=1 => חטיבה עליונה), ולכן העדכון מיושם על כל מורה "
                     "עם in_reform=1 מתאריך זה ואילך. למורים שאינם ברפורמה אין נתון על שלב הלימוד, ולכן "
                     "הם נותרים על 1.2% (שמרני) עד שיתווסף שדה ייעודי.",
         source_doc="גמולי_עוז_מפורטל_מורים", source_ref="עדכון 1.9.2025", priority=91,
         applies_when="in_reform = 1 AND calc_date >= 2025-09-01", overrides="R07_PCT_RATE",
         valid_from="2025-09-01"),
    dict(rule_id="R08_TRANSITION_UNIT", topic="גמול_השתלמות", title="יחידת מעבר לרפורמה",
         description="אם U_pre=16 ויש יתרת שעות מעבר ל-16 יחידות, מוכרת יחידה נוספת מ-R_reform.",
         source_doc="הסכם_עוז_לתמורה", source_ref="סעיף 18(ב)", priority=22, applies_when="in_reform=1 and U_pre=16"),
    dict(rule_id="R08B_PRE_REFORM_EXCLUDED", topic="גמול_השתלמות", title="קורסים מלפני ההצטרפות - לא נספרים שוב",
         description="קורס שתאריך תחילתו לפני תאריך ההצטרפות לעוז לתמורה כבר מגולם ב-U_pre/H_pre (יחידות/שעות "
                     "שהוכרו ערב הרפורמה) - לא נכנס שוב לחישוב השנתי, כדי למנוע ספירה כפולה. כך גם קורס "
                     "שבקובץ \"הקורסים שלי\" אין לו ערך בעמודת \"משתתף בגמולי עוז לתמורה\" - הוא נספר רק "
                     "בחישוב טרום הרפורמה. קורס שגם מסומן \"לא\" בעמודת \"קורס גמולים (לא ברפורמה)\" "
                     "לא נצבר לגמולים בכלל ולא נטען מהקובץ.",
         source_doc="לוגיקת_מערכת", source_ref="מניעת ספירה כפולה מול U_pre/H_pre", priority=16,
         applies_when="(reform_start_date is set AND course.start_date < reform_start_date) OR counts_for_oz=0"),
    dict(rule_id="R08C_PRE_REFORM_COMPUTED", topic="גמול_השתלמות", title="יחידות/שעות ערב הרפורמה",
         description="אם הוזנו יחידות/שעות שהוכרו ערב הרפורמה (U_pre/H_pre) - משתמשים בהן. אחרת הן מחושבות "
                     "מהקורסים שתחילתם לפני ההצטרפות לעוז לתמורה (ומקורסים שלא נספרים לעוז לתמורה), לפי חוקי חוזר המנכ\"ל שלפני הרפורמה: "
                     "112 שעות ליחידה (168 לגמלאי), הרכב עם ציון, פסילה ללא אישור מוקדם ותקרה 16 - "
                     "ללא תקרת יחידה לשנה וללא תקרת השתלמות בית-ספרית (שתיהן מהסכם עוז לתמורה).",
         source_doc="חוזר_מנכל_השתלמויות", source_ref="3.4.2 + לוגיקת מערכת", priority=17,
         applies_when="in_reform=1 AND U_pre=0 AND H_pre=0 AND יש קורסים לפני reform_start_date"),
    dict(rule_id="R09_THRESH_18", topic="גמול_השתלמות", title="סף הכרה קורסים ליחידה 18",
         description="קורס מוכר ליחידה ה-18 רק אם תחילתו לא לפני max(תשע\"ב, R_reform).",
         source_doc="הסכם_עוז_לתמורה", source_ref="סעיף 18(ג)-(ד)", priority=40, applies_when="in_reform=1"),
    dict(rule_id="R10_THRESH_19", topic="גמול_השתלמות", title="סף הכרה קורסים ליחידה 19",
         description="קורס מוכר ליחידה ה-19 רק אם תחילתו לא לפני max(תשע\"ט, R_reform) וגם calc_date>=תש\"פ.",
         source_doc="נוהל_עוז_לתמורה_תשפ", source_ref="סעיפים 5-6", priority=41, applies_when="in_reform=1"),
    dict(rule_id="R11_ANNUAL_CAP", topic="גמול_השתלמות", title="תקרה שנתית 1 יחידה + carry",
         description="בכל שנה נוצרת לכל היותר יחידה 1 חדשה; שעות עודפות עוברות (carry) לשנה הבאה.",
         source_doc="הסכם_עוז_לתמורה", source_ref="סעיף 18(ד)", priority=50, applies_when="תמיד"),
    dict(rule_id="R12_SABBATICAL_EXEMPT", topic="גמול_השתלמות", title="פטור שבתון/חצי-שבתון/חלת",
         description="בשנה בה המורה בשבתון/חצי-שבתון/חל\"ת, לא חלה תקרת 1 יחידה/שנה.",
         source_doc="הסכם_עוז_לתמורה", source_ref="סעיף 18(ה)", priority=51,
         applies_when="status in (שבתון,חצי-שבתון,חלת)", overrides="R11_ANNUAL_CAP"),
    dict(rule_id="R13_SCHOOL_HOURS_CAP", topic="גמול_השתלמות", title="תקרת שעות השתלמות בית-ספרית",
         description="עד 30 שעות בשנה מהשתלמות בית-ספרית נספרות; שאר השעות מאותו מקור לא נספרות.",
         source_doc="הסכם_עוז_לתמורה", source_ref="סעיף 18(ו)", priority=45, applies_when="source_type=school_based"),
    dict(rule_id="R14_ADVANCED_DEGREE", topic="גמול_השתלמות", title="צבירה בלתי מוגבלת - תואר מתקדם",
         description="לימודי תואר מתקדם/הרחבת הוראה/תואר נוסף: צבירה ללא הגבלה, תשלום גמול אחד בשנה.",
         source_doc="חוזר_מנכל_השתלמויות", source_ref="6.9.1 / הסכם עוז לתמורה", priority=46,
         applies_when="course.is_advanced_degree_track = 1"),
    dict(rule_id="R16_DUAL_REFORM", topic="גמול_השתלמות", title="הכרה כפולה אופק חדש",
         description="שעות מוכרות במקביל לשתי הרפורמות אם המורה מועסק בשתיהן.",
         source_doc="נוהל_עוז_לתמורה", source_ref="סעיף 12", priority=95,
         applies_when="dual_reform_ofek_hadash = 1"),
    dict(rule_id="R17_NO_PREAPPROVAL", topic="גמול_השתלמות", title="פסילה - אין אישור מוקדם",
         description="קורס שדרש אישור מפקח מראש ולא קיבל - שעותיו לא מוכרות כלל.",
         source_doc="חוזר_מנכל_השתלמויות", source_ref="5.2.5.3", priority=15, applies_when="course.pre_approved=0"),
    dict(rule_id="R21_PRIORITY_OVERRIDE", topic="גמול_השתלמות", title="עדיפות עוז לתמורה על חוזר כללי",
         description="למורים בתנאי הרפורמה, הסכם עוז לתמורה גובר על חוזר המנכ\"ל הכללי בכל סתירה.",
         source_doc="הסכם_עוז_לתמורה", source_ref="סינתזה", priority=1, applies_when="in_reform=1"),
]

# ==================================================================
# פרמטרים מספריים - הערכים הסופיים שהחוקים משתמשים בהם (שעות ליחידה,
# תקרות, אחוזים). התנאי (מי זכאי לאיזה ערך) נשאר בקוד; רק המספר עצמו
# כאן, כדי שאפשר יהיה לעדכן אותו בלי לגעת ב-engine.py.
# ==================================================================

CONSTITUTIONS_METADATA = [
    dict(code="gimul_hishtalmut", name="חוקת גמולי השתלמות",
         description="חוקי חישוב גמול השתלמות לעובדי הוראה - הסכם עוז לתמורה (סעיף 18) וחוזרי מנכ\"ל השתלמויות."),
]
# כל החוקים ב-RULES_METADATA שייכים לחוקה הזו (אלא אם חוק מגדיר "constitution" אחר).
DEFAULT_CONSTITUTION_CODE = "gimul_hishtalmut"

PARAMETERS_METADATA = [
    dict(rule_id="R01_UNIT_HOURS", param_name="unit_hours", param_value=112,
         valid_from=None, valid_to=None),
    dict(rule_id="R01B_GRADED_COMPOSITION", param_name="min_graded_fraction", param_value=0.5,
         valid_from=None, valid_to=None),
    dict(rule_id="R15_PENSIONER_RATE", param_name="unit_hours", param_value=168,
         valid_from=None, valid_to=None),
    dict(rule_id="R02_BASE_CAP", param_name="units_cap", param_value=16,
         valid_from=None, valid_to=None),
    dict(rule_id="R03_REFORM_CAP", param_name="units_cap", param_value=18,
         valid_from=None, valid_to=SCHOOL_YEAR_START['תש"פ']),
    dict(rule_id="R03_REFORM_CAP", param_name="units_cap", param_value=19,
         valid_from=SCHOOL_YEAR_START['תש"פ'], valid_to=None),
    dict(rule_id="R11_ANNUAL_CAP", param_name="units_per_year_cap", param_value=1,
         valid_from=None, valid_to=None),
    dict(rule_id="R13_SCHOOL_HOURS_CAP", param_name="school_based_annual_hours_cap", param_value=30,
         valid_from=None, valid_to=None),
    dict(rule_id="R04_DOUBLE_DEGREE", param_name="units", param_value=8.75,
         valid_from=None, valid_to=None),
    dict(rule_id="R05_BEINAYIM", param_name="units", param_value=4.0,
         valid_from=None, valid_to=None),
    dict(rule_id="R04B_ADDITIONAL_DOUBLE_DEGREE", param_name="units", param_value=4.0,
         valid_from=None, valid_to=None),
    dict(rule_id="R06_TOTAL_MAX", param_name="total_units_cap", param_value=24.75,
         valid_from=None, valid_to=None),
    dict(rule_id="R07_PCT_RATE", param_name="pct_per_unit", param_value=1.2,
         valid_from=None, valid_to=None),
    dict(rule_id="R07B_PCT_RATE_REFORM", param_name="pct_per_unit", param_value=1.3,
         valid_from="2025-09-01", valid_to=None),
]


def init_db(reset: bool = True):
    conn = get_connection()
    if reset:
        conn.executescript("""
            DROP TABLE IF EXISTS payslips CASCADE;
            DROP TABLE IF EXISTS teacher_documents CASCADE;
            DROP TABLE IF EXISTS calculation_runs CASCADE;
            DROP TABLE IF EXISTS employment_status_by_year CASCADE;
            DROP TABLE IF EXISTS courses CASCADE;
            DROP TABLE IF EXISTS rule_parameters CASCADE;
            DROP TABLE IF EXISTS rule_conditions CASCADE;
            DROP TABLE IF EXISTS teachers CASCADE;
            DROP TABLE IF EXISTS rules CASCADE;
            DROP TABLE IF EXISTS constitution_change_log CASCADE;
            DROP TABLE IF EXISTS constitution_versions CASCADE;
            DROP TABLE IF EXISTS agreements CASCADE;
            DROP TABLE IF EXISTS constitutions CASCADE;
        """)
    schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
    with open(schema_path, encoding="utf-8") as f:
        conn.executescript(f.read())

    # מיגרציה קלה: DB ישן (לפני שנוסף access_token) לא יקבל את העמודה
    # דרך CREATE TABLE IF NOT EXISTS - צריך להוסיף אותה ולמלא לשורות קיימות.
    cols = [r["column_name"] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='teachers'"
    ).fetchall()]
    if "access_token" not in cols:
        conn.execute("ALTER TABLE teachers ADD COLUMN access_token TEXT UNIQUE")
    for r in conn.execute("SELECT teacher_id FROM teachers WHERE access_token IS NULL").fetchall():
        conn.execute("UPDATE teachers SET access_token=%s WHERE teacher_id=%s",
                      (secrets.token_urlsafe(16), r["teacher_id"]))
    if "has_additional_double_degree_gimul" not in cols:
        conn.execute("ALTER TABLE teachers ADD COLUMN has_additional_double_degree_gimul INTEGER DEFAULT 0")
    for col, ddl in (
        ("reform_start_date", "ALTER TABLE teachers ADD COLUMN reform_start_date TEXT"),
        ("certification_date", "ALTER TABLE teachers ADD COLUMN certification_date TEXT"),
        ("double_degree_start_date", "ALTER TABLE teachers ADD COLUMN double_degree_start_date TEXT"),
        ("beinayim_start_date", "ALTER TABLE teachers ADD COLUMN beinayim_start_date TEXT"),
        ("additional_double_degree_start_date", "ALTER TABLE teachers ADD COLUMN additional_double_degree_start_date TEXT"),
    ):
        if col not in cols:
            conn.execute(ddl)

    course_cols = [r["column_name"] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='courses'"
    ).fetchall()]
    if "end_date" not in course_cols:
        conn.execute("ALTER TABLE courses ADD COLUMN end_date TEXT")
    if "description" not in course_cols:
        conn.execute("ALTER TABLE courses ADD COLUMN description TEXT")
    if "hours_graded" not in course_cols:
        conn.execute("ALTER TABLE courses ADD COLUMN hours_graded REAL")
    if "hours_ungraded" not in course_cols:
        conn.execute("ALTER TABLE courses ADD COLUMN hours_ungraded REAL")
    if "counts_for_oz" not in course_cols:
        conn.execute("ALTER TABLE courses ADD COLUMN counts_for_oz INTEGER DEFAULT 1")

    rule_cols = [r["column_name"] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='rules'"
    ).fetchall()]
    if "constitution_id" not in rule_cols:
        conn.execute("ALTER TABLE rules ADD COLUMN constitution_id INTEGER REFERENCES constitutions(constitution_id)")
    for col, ddl in (
        ("action", "ALTER TABLE rules ADD COLUMN action TEXT"),
        ("result_value", "ALTER TABLE rules ADD COLUMN result_value TEXT"),
        ("logic_seeded", "ALTER TABLE rules ADD COLUMN logic_seeded INTEGER NOT NULL DEFAULT 0"),
    ):
        if col not in rule_cols:
            conn.execute(ddl)

    # מכאן והלאה ה-DB הוא מקור האמת של החוקה (נערכת מהבקאופיס): הקוד רק זורע
    # חוקות/חוקים/פרמטרים שעוד לא קיימים, ולא דורס ערכים שנערכו. שינוי תיאור
    # או ערך של חוק קיים ב-RULES_METADATA/PARAMETERS_METADATA לא יגיע ל-DB -
    # עורכים אותו בבקאופיס.
    cur = conn.cursor()
    for c in CONSTITUTIONS_METADATA:
        cur.execute("""
            INSERT INTO constitutions (code, name, description)
            VALUES (%(code)s, %(name)s, %(description)s)
            ON CONFLICT (code) DO NOTHING
        """, c)
    constitution_ids = {r["code"]: r["constitution_id"] for r in conn.execute(
        "SELECT code, constitution_id FROM constitutions").fetchall()}

    for r in RULES_METADATA:
        params = {
            "rule_id": r["rule_id"], "topic": r["topic"], "title": r["title"],
            "description": r["description"], "source_doc": r["source_doc"],
            "source_ref": r.get("source_ref"), "priority": r["priority"],
            "applies_when": r.get("applies_when"), "valid_from": r.get("valid_from"),
            "overrides": r.get("overrides"),
            "constitution_id": constitution_ids[r.get("constitution", DEFAULT_CONSTITUTION_CODE)],
        }
        cur.execute("""
            INSERT INTO rules
            (rule_id, topic, title, description, source_doc, source_ref, priority,
             applies_when, valid_from, valid_to, overrides, active, constitution_id)
            VALUES (%(rule_id)s, %(topic)s, %(title)s, %(description)s, %(source_doc)s, %(source_ref)s,
                    %(priority)s, %(applies_when)s, %(valid_from)s, NULL, %(overrides)s, 1, %(constitution_id)s)
            ON CONFLICT (rule_id) DO NOTHING
        """, params)
        # חוקים שנזרעו לפני שהייתה טבלת חוקות - משייכים לחוקה שלהם
        cur.execute("UPDATE rules SET constitution_id=%(constitution_id)s "
                    "WHERE rule_id=%(rule_id)s AND constitution_id IS NULL", params)

    # פרמטר נזרע רק אם אין ב-DB אף שורה (תקופה) עבור rule_id + param_name
    existing = {(r["rule_id"], r["param_name"]) for r in conn.execute(
        "SELECT DISTINCT rule_id, param_name FROM rule_parameters").fetchall()}
    for p in PARAMETERS_METADATA:
        if (p["rule_id"], p["param_name"]) in existing:
            continue
        cur.execute("""
            INSERT INTO rule_parameters (rule_id, param_name, param_value, valid_from, valid_to)
            VALUES (%(rule_id)s, %(param_name)s, %(param_value)s, %(valid_from)s, %(valid_to)s)
        """, p)

    seed_rule_logic(conn)

    conn.commit()
    conn.close()
    print(f"DB initialized with {len(RULES_METADATA)} rules and {len(PARAMETERS_METADATA)} parameters.")


def get_param(conn, rule_id, param_name, calc_date):
    row = conn.execute("""
        SELECT param_value FROM rule_parameters
        WHERE rule_id=%s AND param_name=%s
          AND (valid_from IS NULL OR valid_from <= %s)
          AND (valid_to IS NULL OR valid_to > %s)
        ORDER BY valid_from DESC
        LIMIT 1
    """, (rule_id, param_name, calc_date, calc_date)).fetchone()
    if row is None:
        raise ValueError(f"Missing parameter {rule_id}.{param_name} for calc_date={calc_date}")
    return row["param_value"]


# ==================================================================
# שלב 2: מנוע החישוב
# ==================================================================

@dataclass
class Trace:
    steps: list = field(default_factory=list)

    def log(self, rule_id, message, value=None):
        self.steps.append({"rule_id": rule_id, "message": message, "value": value})


def get_teacher(conn, teacher_id):
    row = conn.execute("SELECT * FROM teachers WHERE teacher_id=%s", (teacher_id,)).fetchone()
    return dict(row) if row else None


def get_courses(conn, teacher_id):
    cur = conn.execute("SELECT * FROM courses WHERE teacher_id=%s ORDER BY start_date", (teacher_id,))
    return [dict(r) for r in cur.fetchall()]


def get_status_by_year(conn, teacher_id):
    cur = conn.execute("SELECT school_year, status FROM employment_status_by_year WHERE teacher_id=%s", (teacher_id,))
    return {row["school_year"]: row["status"] for row in cur.fetchall()}


def _course_label(c):
    """תווית קורס לפירוט: שם הקורס במקום course_id הפנימי (שאין לו משמעות למורה)."""
    desc = (c.get("description") or "").strip()
    return f"קורס \"{desc}\"" if desc else "קורס"


def _is_pre_reform_course(teacher, c):
    """קורס ששייך לחישוב טרום הרפורמה: התחיל לפני ההצטרפות לעוז לתמורה, או שלפי
    הפורטל הוא לא משתתף בגמולי עוז לתמורה בכלל (counts_for_oz=0)."""
    if c.get("counts_for_oz") == 0:
        return True
    return bool(teacher["reform_start_date"]) and c["start_date"] < teacher["reform_start_date"]


def _compute_pre_reform_from_courses(conn, pre_courses, H_unit, calc_date, trace):
    """
    R08C: גזירת U_pre/H_pre מקורסים שלפני ההצטרפות לעוז לתמורה, לפי חוזר המנכ"ל
    (החוקים שלפני הרפורמה): R17 פסילה ללא אישור, R01/R15 שעות ליחידה, R01B הרכב
    עם ציון, R02 תקרה 16. בלי R11 (יחידה בשנה) ובלי R13 (תקרה בית-ספרית) - שניהם
    מהסכם עוז לתמורה עצמו. מסלול תואר מתקדם (R14) לא נכלל, כמו לפני כן.
    מחזיר (U_pre, H_pre, H_pre_graded).
    """
    total = graded = 0.0
    for c in pre_courses:
        if not c["pre_approved"]:
            trace.log("R17_NO_PREAPPROVAL", f"{_course_label(c)} (לפני הרפורמה) נפסל - אין אישור מוקדם")
            continue
        if c["is_advanced_degree_track"]:
            continue
        if c["hours_graded"] is not None or c["hours_ungraded"] is not None:
            course_graded = c["hours_graded"] or 0
        else:
            course_graded = c["hours"]
        if course_graded and c["grade"] is not None and c["grade"] < 60:
            course_graded = 0
        total += c["hours"]
        graded += min(course_graded, c["hours"])

    base_cap = int(get_param(conn, "R02_BASE_CAP", "units_cap", calc_date))
    min_graded_fraction = get_param(conn, "R01B_GRADED_COMPOSITION", "min_graded_fraction", calc_date)
    by_hours = math.floor(total / H_unit)
    by_composition = math.floor(graded / (H_unit * min_graded_fraction)) if min_graded_fraction > 0 else by_hours
    U_pre = min(by_hours, by_composition, base_cap)
    H_pre = total - U_pre * H_unit
    H_pre_graded = max(0.0, graded - U_pre * H_unit * (graded / total)) if total else 0.0
    trace.log("R08C_PRE_REFORM_COMPUTED",
              f"לא הוזנו יחידות/שעות ערב הרפורמה - חושבו מ-{len(pre_courses)} קורסים שלפני ההצטרפות "
              f"לפי חוזר המנכ\"ל: {total:.0f} שעות ({graded:.0f} עם ציון) => {U_pre} יח' "
              f"(תקרה {base_cap}), ויתרה של {H_pre:.0f} שעות", U_pre)
    return U_pre, H_pre, H_pre_graded


def compute_gimul_hishtalmut(teacher_id: str, calc_date: str) -> dict:
    """
    מריץ את מנוע חוקי גמול ההשתלמות על מורה נתון, עד לתאריך calc_date.
    מחזיר dict עם הפלט המלא + trace (audit trail).
    """
    conn = get_connection()
    trace = Trace()

    teacher = get_teacher(conn, teacher_id)
    if teacher is None:
        raise ValueError(f"Teacher {teacher_id} not found")

    courses = get_courses(conn, teacher_id)
    status_by_year = get_status_by_year(conn, teacher_id)

    # --- R01/R15: קביעת H_unit ---
    if teacher["is_pensioner"]:
        H_unit = int(get_param(conn, "R15_PENSIONER_RATE", "unit_hours", calc_date))
        trace.log("R15_PENSIONER_RATE", f"מורה גמלאי - יחידת גמול = {H_unit} שעות", H_unit)
    else:
        H_unit = int(get_param(conn, "R01_UNIT_HOURS", "unit_hours", calc_date))
        trace.log("R01_UNIT_HOURS", f"יחידת גמול = {H_unit} שעות", H_unit)

    # --- R02/R03/R21: קביעת התקרה החלה ---
    calc_dt = datetime.fromisoformat(calc_date).date()
    tashpa = datetime.fromisoformat(SCHOOL_YEAR_START['תש"פ']).date()
    if teacher["in_reform"]:
        trace.log("R21_PRIORITY_OVERRIDE", "מורה בתנאי עוז לתמורה - הסכם עוז לתמורה גובר על החוזר הכללי")
        U_cap = int(get_param(conn, "R03_REFORM_CAP", "units_cap", calc_date))
        note = "מתש\"פ ואילך" if calc_dt >= tashpa else "לפני תש\"פ"
        trace.log("R03_REFORM_CAP", f"תקרה מעודכנת ({note})", U_cap)
    else:
        U_cap = int(get_param(conn, "R02_BASE_CAP", "units_cap", calc_date))
        trace.log("R02_BASE_CAP", "תקרה כללית (לא ברפורמה)", U_cap)

    # --- R08: יחידת מעבר לרפורמה ---
    # H_pre = שעות שנצברו ערב הרפורמה מעבר ליחידות שכבר הוכרו (U_pre) - יתרה, לא סה"כ שעות.
    U_pre = teacher["u_pre"] or 0
    H_pre = (teacher["h_pre"] or 0) if teacher["in_reform"] else 0
    H_pre_graded = H_pre  # ערכים שהוזנו ידנית - אין פירוט, נחשבים כעם ציון
    pre_courses = [c for c in courses if _is_pre_reform_course(teacher, c)]
    if teacher["in_reform"] and not U_pre and not H_pre and pre_courses:
        U_pre, H_pre, H_pre_graded = _compute_pre_reform_from_courses(conn, pre_courses, H_unit, calc_date, trace)
    elif teacher["in_reform"] and (U_pre or H_pre):
        trace.log("R08C_PRE_REFORM_COMPUTED",
                  f"יחידות/שעות ערב הרפורמה נלקחו מהערכים שהוזנו: {U_pre:.0f} יח', {H_pre:.0f} שעות", U_pre)
    U_base = U_pre
    pre_reform_carry = H_pre
    if teacher["in_reform"] and U_pre == 16 and H_pre > 0:
        U_base = 17
        pre_reform_carry = 0  # השעות העודפות נוצלו ליחידת המעבר
        trace.log("R08_TRANSITION_UNIT",
                   f"זוהתה יתרת שעות מעל 16 יחידות (H_pre={H_pre:.0f}) - הוכרה יחידה 17-ית",
                   U_base)
    else:
        trace.log("R08_TRANSITION_UNIT", f"אין זכאות ליחידת מעבר - בסיס נשאר {U_base}", U_base)

    # --- R09/R10/R17/R13: סינון קורסים ---
    reform_start = SCHOOL_YEAR_START.get(teacher["reform_start_year"]) if teacher["reform_start_year"] else None
    thresh_18 = max(filter(None, [SCHOOL_YEAR_START.get('תשע"ב'), reform_start])) if teacher["in_reform"] else None
    thresh_19 = max(filter(None, [SCHOOL_YEAR_START.get('תשע"ט'), reform_start])) if teacher["in_reform"] else None

    school_hours_cap = get_param(conn, "R13_SCHOOL_HOURS_CAP", "school_based_annual_hours_cap", calc_date)

    accepted_hours_by_year = {}
    accepted_graded_hours_by_year = {}
    accepted_ungraded_hours_by_year = {}
    school_hours_by_year = {}
    advanced_degree_hours = 0.0
    rejected = []

    for c in courses:
        if not c["pre_approved"]:
            rejected.append({"course_id": c["course_id"], "reason": "אין אישור מוקדם מהמפקח (R17)"})
            trace.log("R17_NO_PREAPPROVAL", f"{_course_label(c)} נפסל - אין אישור מוקדם")
            continue

        if c.get("counts_for_oz") == 0:
            trace.log("R08B_PRE_REFORM_EXCLUDED",
                      f"{_course_label(c)} - לא משתתף בגמולי עוז לתמורה לפי הפורטל - נספר רק בחישוב "
                      f"היחידות/שעות ערב הרפורמה, לא בחישוב השנתי")
            continue

        if teacher["reform_start_date"] and c["start_date"] < teacher["reform_start_date"]:
            trace.log("R08B_PRE_REFORM_EXCLUDED",
                      f"{_course_label(c)} - מתאריך {c['start_date']}, לפני ההצטרפות לעוז לתמורה "
                      f"({teacher['reform_start_date']}) - כבר מגולם ביחידות/שעות שהוכרו ערב הרפורמה, לא נספר שוב")
            continue

        hours = c["hours"]
        sy = school_year_of(c["start_date"])

        if c["is_advanced_degree_track"]:
            advanced_degree_hours += hours
            trace.log("R14_ADVANCED_DEGREE", f"{_course_label(c)} - מסלול תואר מתקדם, {hours} שעות", hours)
            continue  # לא נכנס לחישוב הרגיל - יש לו מסלול נפרד

        if c["source_type"] == "school_based":
            prev = school_hours_by_year.get(sy, 0)
            allowed = max(0, school_hours_cap - prev)
            counted = min(hours, allowed)
            if counted < hours:
                trace.log("R13_SCHOOL_HOURS_CAP",
                          f"{_course_label(c)} - נחתך מ-{hours} ל-{counted} שעות (תקרת {school_hours_cap:.0f}/שנה)")
            school_hours_by_year[sy] = prev + counted
            hours = counted

        accepted_hours_by_year[sy] = accepted_hours_by_year.get(sy, 0) + hours

        # פיצול עם/בלי ציון לצורך R01B (הרכב 56/112) ולפירוט היתרה - לא משפיע
        # על סך השעות/יחידות שנספרות, רק על כמה מהן "עומדות בתנאי" ההרכב.
        if c["hours_graded"] is not None or c["hours_ungraded"] is not None:
            course_graded = c["hours_graded"] or 0
            course_ungraded = c["hours_ungraded"] or 0
        else:
            # נתון ישן מלפני שהתווסף הפיצול hours_graded/hours_ungraded (וגם הדגל
            # graded לא הוזן משמעותית עבורו - ברירת המחדל בסכימה היא 0 לכולם).
            # מטופל כמו ציון לא ידוע: בהנחת עמידה בתנאי, לא כ"בלי ציון" - אחרת
            # קורסים ישנים לגיטימיים היו מאבדים זכאות ליחידה בגלל חוסר בנתון, לא בפועל.
            course_graded = c["hours"]
            course_ungraded = 0
        # הפיצול עם/בלי ציון לא יכול לעלות על השעות שנספרות בפועל. זה קורה כשתקרת
        # השתלמות בית-ספרית צמצמה את hours, או כשעמודת עוז לתמורה בקובץ קטנה
        # מהיקף השעות הכללי (למשל 60 עם ציון, מהן 30 בעוז). אחרת היחס עם ציון
        # עולה על 100% ונוצרת יתרה "בלי ציון" שלא קיימת.
        split_total = course_graded + course_ungraded
        base = max(c["hours"] or 0, split_total)
        if base and hours < base:
            ratio = hours / base
            course_graded *= ratio
            course_ungraded *= ratio

        # ציון רשום מתחת ל-60: השעות עם ציון של הקורס הזה לא "עומדות בתנאי" ל-R01B,
        # אך עדיין נספרות בסך הכל (accepted_hours_by_year, כבר עודכן למעלה).
        if course_graded and c["grade"] is not None and c["grade"] < 60:
            trace.log("R01B_GRADED_COMPOSITION",
                      f"{_course_label(c)} - ציון {c['grade']:.0f} מתחת ל-60, {course_graded:.0f} שעות עם "
                      f"ציון לא נספרות לדרישת ההרכב (56/112)", 0)
            course_ungraded += course_graded
            course_graded = 0

        accepted_graded_hours_by_year[sy] = accepted_graded_hours_by_year.get(sy, 0) + course_graded
        accepted_ungraded_hours_by_year[sy] = accepted_ungraded_hours_by_year.get(sy, 0) + course_ungraded

    # --- R11/R12: חישוב שנתי עם carry ופטור שבתון ---
    units_per_year_cap = int(get_param(conn, "R11_ANNUAL_CAP", "units_per_year_cap", calc_date))
    min_graded_fraction = get_param(conn, "R01B_GRADED_COMPOSITION", "min_graded_fraction", calc_date)
    tashpa_start = SCHOOL_YEAR_START['תש"פ']
    delta_u_by_year = {}
    # יתרת השעות מערב הרפורמה נכנסת כיתרת פתיחה. כשהוזנה ידנית אין פירוט עם/בלי
    # ציון, והן כבר הוכרו לפני הרפורמה - לכן נחשבות כשעות עם ציון. כשחושבה
    # מהקורסים (R08C), הפירוט ידוע.
    carry = float(pre_reform_carry)
    carry_graded = min(float(H_pre_graded), carry)
    carry_ungraded = carry - carry_graded
    if pre_reform_carry > 0:
        trace.log("R08_TRANSITION_UNIT",
                  f"יתרת פתיחה: {pre_reform_carry:.0f} שעות שנצברו ערב הרפורמה מעבר ליחידות שהוכרו", pre_reform_carry)
    running_total = U_base
    year_breakdown = []
    for sy in sorted(accepted_hours_by_year.keys(), key=lambda y: SCHOOL_YEAR_START.get(y, "9999")):
        available = accepted_hours_by_year[sy] + carry
        available_graded = accepted_graded_hours_by_year.get(sy, 0) + carry_graded
        available_ungraded = accepted_ungraded_hours_by_year.get(sy, 0) + carry_ungraded
        status = status_by_year.get(sy, "רגיל")
        is_exempt_year = status in ("שבתון", "חצי-שבתון", "חלת")
        potential_units_by_hours = math.floor(available / H_unit)

        # --- R01B: הרכב 56/112 - לפחות מחצית משעות היחידה חייבות להיות עם ציון עובר ---
        min_graded_per_unit = H_unit * min_graded_fraction
        potential_units_by_composition = (
            math.floor(available_graded / min_graded_per_unit) if min_graded_per_unit > 0 else potential_units_by_hours
        )
        potential_units = min(potential_units_by_hours, potential_units_by_composition)
        if potential_units < potential_units_by_hours:
            trace.log("R01B_GRADED_COMPOSITION",
                      f"שנה {sy} - יש {available:.0f} שעות (כ-{potential_units_by_hours} יח'), אך רק "
                      f"{available_graded:.0f} מהן עם ציון עובר (נדרש לפחות {min_graded_fraction*100:.0f}% "
                      f"מכל יחידה) - נחתך ל-{potential_units} יח'", potential_units)

        delta = potential_units if is_exempt_year else min(units_per_year_cap, potential_units)

        # --- R09/R10: סף הכרה קורסים ליחידות 18/19 (רק למורה ברפורמה) ---
        year_start = SCHOOL_YEAR_START.get(sy, "9999")
        max_allowed = running_total + delta
        if thresh_18 is not None and running_total < 18 and year_start < thresh_18:
            max_allowed = min(max_allowed, 17)
        if thresh_19 is not None and running_total < 19 and (year_start < thresh_19 or calc_date < tashpa_start):
            max_allowed = min(max_allowed, 18)
        capped_delta = max(0, min(delta, max_allowed - running_total))
        threshold_capped = capped_delta < delta
        if threshold_capped:
            rule_id = "R09_THRESH_18" if max_allowed <= 17 else "R10_THRESH_19"
            trace.log(rule_id,
                      f"שנה {sy} - קורסי השנה מוקדמים מדי לזכאות ליחידה {int(max_allowed) + 1}, נחתך מ-{delta} ל-{capped_delta} יח'",
                      capped_delta)
        delta = capped_delta

        carry = available - delta * H_unit
        delta_u_by_year[sy] = delta
        running_total += delta

        # --- הודעה שנתית ידידותית (R11/R12): כמה נלמד, כמה הוכר בפועל, כמה נצבר הלאה ---
        hours_used = delta * H_unit
        # פירוט היתרה (carry) עם/בלי ציון - החלוקה בין הקטגוריות כשצורכים
        # שעות ליחידה נעשית לפי היחס שלהן בין הזמינות באותה שנה (לא משפיעה
        # על מספר היחידות, רק על איך היתרה שנותרת מיוחסת לכל קטגוריה).
        frac_graded = (available_graded / available) if available > 0 else 0.0
        carry_graded = available_graded - hours_used * frac_graded
        carry_ungraded = available_ungraded - hours_used * (1 - frac_graded)
        if is_exempt_year:
            msg = (f"שנה {sy} (סטטוס={status}) - נלמדו {available:.0f} שעות (כ-{potential_units_by_hours} יח'), "
                   f"ללא תקרת {units_per_year_cap} יח'/שנה: הוכרו {delta} יח' ({hours_used:.0f} שעות)")
        elif potential_units > delta and not threshold_capped:
            msg = (f"שנה {sy} - נלמדו {available:.0f} שעות (כ-{potential_units_by_hours} יח'), אך לא ניתן לתרגם לגמול את כל "
                   f"היחידות (תקרה של {units_per_year_cap} יח'/שנה) - הוכרה {delta} יח' ({hours_used:.0f} שעות)")
        else:
            msg = f"שנה {sy} - נלמדו {available:.0f} שעות (כ-{potential_units_by_hours} יח') - הוכרה {delta} יח' ({hours_used:.0f} שעות)"
        if carry > 0:
            msg += f", ונצברו {carry:.0f} שעות לשנה הבאה"
        trace.log("R12_SABBATICAL_EXEMPT" if is_exempt_year else "R11_ANNUAL_CAP", msg, delta)

        year_breakdown.append({
            "school_year": sy,
            "status": status,
            "hours_graded_available": round(available_graded, 2),
            "hours_ungraded_available": round(available_ungraded, 2),
            "units_earned": delta,
            "carry_graded": round(carry_graded, 2),
            "carry_ungraded": round(carry_ungraded, 2),
        })

    if delta_u_by_year:
        trace.log("R11_ANNUAL_CAP",
                  f"סה\"כ נותרו {carry:.0f} שעות שלא תורגמו ליחידה ({carry_graded:.0f} עם ציון, "
                  f"{carry_ungraded:.0f} בלי ציון) - יילקחו בחשבון בחישוב הגמול הבא", carry)

    U_regular_earned = U_base + sum(delta_u_by_year.values())
    U_regular = min(U_regular_earned, U_cap)
    if U_regular_earned > U_cap:
        trace.log("R03_REFORM_CAP" if teacher["in_reform"] else "R02_BASE_CAP",
                  f"נצברו {U_regular_earned} יחידות אך נחתך לתקרה {U_cap}", U_regular)

    # תואר מתקדם - גמול אחד בשנה, ללא הגבלת צבירה (מוצג בנפרד, לא בתקרה)
    advanced_units_available = advanced_degree_hours / H_unit if advanced_degree_hours else 0

    # --- R04/R04B/R05: כפל תואר / כפל תואר נוסף / ביניים ---
    U_dd_or_mid = 0.0
    if teacher["has_double_degree_gimul"]:
        U_dd_or_mid = get_param(conn, "R04_DOUBLE_DEGREE", "units", calc_date)
        trace.log("R04_DOUBLE_DEGREE", "מוכר גמול כפל תואר", U_dd_or_mid)
        if teacher["has_additional_double_degree_gimul"]:
            additional_units = get_param(conn, "R04B_ADDITIONAL_DOUBLE_DEGREE", "units", calc_date)
            U_dd_or_mid += additional_units
            trace.log("R04B_ADDITIONAL_DOUBLE_DEGREE",
                      "מוכר גם כפל תואר נוסף (\"כפל שני\") - בנוסף לכפל תואר", additional_units)
    elif teacher["has_beinayim_gimul"]:
        U_dd_or_mid = get_param(conn, "R05_BEINAYIM", "units", calc_date)
        trace.log("R05_BEINAYIM", "מוכר גמול ביניים", U_dd_or_mid)

    # --- R06/R06B: תקרה כוללת (רק למי שאינו בעוז לתמורה) ---
    if teacher["in_reform"]:
        U_total = U_regular + U_dd_or_mid
        if U_dd_or_mid:
            trace.log("R06B_NO_CAP_REFORM",
                      "מורה בעוז לתמורה - יחידות כפל תואר/ביניים ניתנות בנוסף לתקרת הרפורמה, ללא תקרת 24.75",
                      U_total)
    else:
        total_units_cap = get_param(conn, "R06_TOTAL_MAX", "total_units_cap", calc_date)
        U_total = min(U_regular + U_dd_or_mid, total_units_cap)
        if U_regular + U_dd_or_mid > total_units_cap:
            trace.log("R06_TOTAL_MAX", "נחתך לתקרה הכוללת המקסימלית", total_units_cap)

    # --- R07/R07B: שיעור תשלום ליחידה ---
    if teacher["in_reform"] and calc_date >= "2025-09-01":
        pct_per_unit = get_param(conn, "R07B_PCT_RATE_REFORM", "pct_per_unit", calc_date)
        trace.log("R07B_PCT_RATE_REFORM", "מורה בעוז לתמורה (חטיבה עליונה) מ-1.9.2025 - שיעור מעודכן 1.3%", pct_per_unit)
    else:
        pct_per_unit = get_param(conn, "R07_PCT_RATE", "pct_per_unit", calc_date)
    pct_addition = round(U_total * pct_per_unit, 4)
    trace.log("R07_PCT_RATE", f"{U_total} יח' × {pct_per_unit}% ליחידה", pct_addition)

    # --- R16: הכרה כפולה ---
    if teacher["dual_reform_ofek_hadash"]:
        trace.log("R16_DUAL_REFORM", "המורה מועסק גם באופק חדש - הכרה כפולה בשעות ההשתלמות")

    result = {
        "teacher_id": teacher_id,
        "calc_date": calc_date,
        "H_unit": H_unit,
        "U_cap_applied": U_cap,
        "U_base_from_transition": U_base,
        "U_regular_earned_before_cap": round(U_regular_earned, 3),
        "U_regular": round(U_regular, 3),
        "U_dd_or_mid": U_dd_or_mid,
        "U_total": round(U_total, 3),
        "pct_per_unit": pct_per_unit,
        "pct_salary_addition": pct_addition,
        "advanced_degree_hours_accumulated": advanced_degree_hours,
        "advanced_degree_units_available": round(advanced_units_available, 3),
        "delta_u_by_year": delta_u_by_year,
        "year_breakdown": year_breakdown,
        "pre_reform_carry": round(pre_reform_carry, 2),
        "carry_remaining": round(carry, 2),
        "carry_remaining_graded": round(carry_graded, 2),
        "carry_remaining_ungraded": round(carry_ungraded, 2),
        "rejected_courses": rejected,
        "trace": trace.steps,
    }

    conn.execute("""
        INSERT INTO calculation_runs
        (teacher_id, run_timestamp, calc_date, U_regular, U_dd_or_mid, U_total, pct_addition,
         U_cap_applied, result_json)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (teacher_id, datetime.now().isoformat(), calc_date, result["U_regular"],
          result["U_dd_or_mid"], result["U_total"], result["pct_salary_addition"],
          result["U_cap_applied"], json.dumps(result, ensure_ascii=False)))
    conn.commit()
    conn.close()

    return result


if __name__ == "__main__":
    init_db(reset=True)
