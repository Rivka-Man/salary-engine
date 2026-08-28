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
         source_doc="חוזר_מנכל_השתלמויות", source_ref="הערה 2 לסעיף 7", priority=32,
         applies_when="has_additional_double_degree_gimul flag AND has_double_degree_gimul flag"),
    dict(rule_id="R06_TOTAL_MAX", topic="גמול_השתלמות", title="תקרה כוללת 24.75",
         description="סך כל היחידות (רגילות + כפל תואר/ביניים) לא יעלה על 24.75 בכל מקרה.",
         source_doc="חוזר_מנכל_השתלמויות", source_ref="3.4.2", priority=90, applies_when="תמיד"),
    dict(rule_id="R07_PCT_RATE", topic="גמול_השתלמות", title="שיעור תשלום ליחידה",
         description="כל יחידת גמול = 1.2% מהשכר המשולב.",
         source_doc="חוזר_מנכל_השתלמויות", source_ref="3.4.2 סוף", priority=91, applies_when="תמיד"),
    dict(rule_id="R08_TRANSITION_UNIT", topic="גמול_השתלמות", title="יחידת מעבר לרפורמה",
         description="אם U_pre=16 ויש יתרת שעות מעבר ל-16 יחידות, מוכרת יחידה נוספת מ-R_reform.",
         source_doc="הסכם_עוז_לתמורה", source_ref="סעיף 18(ב)", priority=22, applies_when="in_reform=1 and U_pre=16"),
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

PARAMETERS_METADATA = [
    dict(rule_id="R01_UNIT_HOURS", param_name="unit_hours", param_value=112,
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
]


def init_db(reset: bool = True):
    conn = get_connection()
    if reset:
        conn.executescript("""
            DROP TABLE IF EXISTS calculation_runs CASCADE;
            DROP TABLE IF EXISTS employment_status_by_year CASCADE;
            DROP TABLE IF EXISTS courses CASCADE;
            DROP TABLE IF EXISTS rule_parameters CASCADE;
            DROP TABLE IF EXISTS teachers CASCADE;
            DROP TABLE IF EXISTS rules CASCADE;
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

    course_cols = [r["column_name"] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='courses'"
    ).fetchall()]
    if "end_date" not in course_cols:
        conn.execute("ALTER TABLE courses ADD COLUMN end_date TEXT")
    if "description" not in course_cols:
        conn.execute("ALTER TABLE courses ADD COLUMN description TEXT")

    cur = conn.cursor()
    for r in RULES_METADATA:
        params = {
            "rule_id": r["rule_id"], "topic": r["topic"], "title": r["title"],
            "description": r["description"], "source_doc": r["source_doc"],
            "source_ref": r.get("source_ref"), "priority": r["priority"],
            "applies_when": r.get("applies_when"), "valid_from": r.get("valid_from"),
            "overrides": r.get("overrides"),
        }
        cur.execute("""
            INSERT INTO rules
            (rule_id, topic, title, description, source_doc, source_ref, priority,
             applies_when, valid_from, valid_to, overrides, active)
            VALUES (%(rule_id)s, %(topic)s, %(title)s, %(description)s, %(source_doc)s, %(source_ref)s,
                    %(priority)s, %(applies_when)s, %(valid_from)s, NULL, %(overrides)s, 1)
            ON CONFLICT (rule_id) DO UPDATE SET
                topic=EXCLUDED.topic, title=EXCLUDED.title, description=EXCLUDED.description,
                source_doc=EXCLUDED.source_doc, source_ref=EXCLUDED.source_ref,
                priority=EXCLUDED.priority, applies_when=EXCLUDED.applies_when,
                valid_from=EXCLUDED.valid_from, overrides=EXCLUDED.overrides, active=1
        """, params)

    cur.execute("DELETE FROM rule_parameters")
    for p in PARAMETERS_METADATA:
        cur.execute("""
            INSERT INTO rule_parameters (rule_id, param_name, param_value, valid_from, valid_to)
            VALUES (%(rule_id)s, %(param_name)s, %(param_value)s, %(valid_from)s, %(valid_to)s)
        """, p)

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
    U_base = teacher["u_pre"] or 0
    if teacher["in_reform"] and teacher["u_pre"] == 16 and (teacher["h_pre"] or 0) > 16 * H_unit:
        U_base = 17
        trace.log("R08_TRANSITION_UNIT",
                   f"זוהתה יתרת שעות מעל 16 יחידות (H_pre={teacher['h_pre']}) - הוכרה יחידה 17-ית",
                   U_base)
    else:
        trace.log("R08_TRANSITION_UNIT", f"אין זכאות ליחידת מעבר - בסיס נשאר {U_base}", U_base)

    # --- R09/R10/R17/R13: סינון קורסים ---
    reform_start = SCHOOL_YEAR_START.get(teacher["reform_start_year"]) if teacher["reform_start_year"] else None
    thresh_18 = max(filter(None, [SCHOOL_YEAR_START.get('תשע"ב'), reform_start])) if teacher["in_reform"] else None
    thresh_19 = max(filter(None, [SCHOOL_YEAR_START.get('תשע"ט'), reform_start])) if teacher["in_reform"] else None

    school_hours_cap = get_param(conn, "R13_SCHOOL_HOURS_CAP", "school_based_annual_hours_cap", calc_date)

    accepted_hours_by_year = {}
    school_hours_by_year = {}
    advanced_degree_hours = 0.0
    rejected = []

    for c in courses:
        if not c["pre_approved"]:
            rejected.append({"course_id": c["course_id"], "reason": "אין אישור מוקדם מהמפקח (R17)"})
            trace.log("R17_NO_PREAPPROVAL", f"קורס {c['course_id']} נפסל - אין אישור מוקדם")
            continue

        hours = c["hours"]
        sy = school_year_of(c["start_date"])

        if c["is_advanced_degree_track"]:
            advanced_degree_hours += hours
            trace.log("R14_ADVANCED_DEGREE", f"קורס {c['course_id']} - מסלול תואר מתקדם, {hours} שעות", hours)
            continue  # לא נכנס לחישוב הרגיל - יש לו מסלול נפרד

        if c["source_type"] == "school_based":
            prev = school_hours_by_year.get(sy, 0)
            allowed = max(0, school_hours_cap - prev)
            counted = min(hours, allowed)
            if counted < hours:
                trace.log("R13_SCHOOL_HOURS_CAP",
                          f"קורס {c['course_id']} - נחתך מ-{hours} ל-{counted} שעות (תקרת {school_hours_cap:.0f}/שנה)")
            school_hours_by_year[sy] = prev + counted
            hours = counted

        accepted_hours_by_year[sy] = accepted_hours_by_year.get(sy, 0) + hours

    # --- R11/R12: חישוב שנתי עם carry ופטור שבתון ---
    units_per_year_cap = int(get_param(conn, "R11_ANNUAL_CAP", "units_per_year_cap", calc_date))
    tashpa_start = SCHOOL_YEAR_START['תש"פ']
    delta_u_by_year = {}
    carry = 0.0
    running_total = U_base
    for sy in sorted(accepted_hours_by_year.keys(), key=lambda y: SCHOOL_YEAR_START.get(y, "9999")):
        available = accepted_hours_by_year[sy] + carry
        status = status_by_year.get(sy, "רגיל")
        if status in ("שבתון", "חצי-שבתון", "חלת"):
            delta = math.floor(available / H_unit)
            trace.log("R12_SABBATICAL_EXEMPT", f"שנה {sy} (סטטוס={status}) - ללא תקרת {units_per_year_cap}/שנה: {delta} יח'", delta)
        else:
            delta = min(units_per_year_cap, math.floor(available / H_unit))
            trace.log("R11_ANNUAL_CAP", f"שנה {sy} - עד יחידה {units_per_year_cap}: {delta} יח' (זמין: {available:.0f} שעות)", delta)

        # --- R09/R10: סף הכרה קורסים ליחידות 18/19 (רק למורה ברפורמה) ---
        year_start = SCHOOL_YEAR_START.get(sy, "9999")
        max_allowed = running_total + delta
        if thresh_18 is not None and running_total < 18 and year_start < thresh_18:
            max_allowed = min(max_allowed, 17)
        if thresh_19 is not None and running_total < 19 and (year_start < thresh_19 or calc_date < tashpa_start):
            max_allowed = min(max_allowed, 18)
        capped_delta = max(0, min(delta, max_allowed - running_total))
        if capped_delta < delta:
            rule_id = "R09_THRESH_18" if max_allowed <= 17 else "R10_THRESH_19"
            trace.log(rule_id,
                      f"שנה {sy} - קורסי השנה מוקדמים מדי לזכאות ליחידה {int(max_allowed) + 1}, נחתך מ-{delta} ל-{capped_delta} יח'",
                      capped_delta)
        delta = capped_delta

        carry = available - delta * H_unit
        delta_u_by_year[sy] = delta
        running_total += delta

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

    # --- R06: תקרה כוללת ---
    total_units_cap = get_param(conn, "R06_TOTAL_MAX", "total_units_cap", calc_date)
    U_total = min(U_regular + U_dd_or_mid, total_units_cap)
    if U_regular + U_dd_or_mid > total_units_cap:
        trace.log("R06_TOTAL_MAX", "נחתך לתקרה הכוללת המקסימלית", total_units_cap)

    # --- R07: שיעור תשלום ---
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
        "pct_salary_addition": pct_addition,
        "advanced_degree_hours_accumulated": advanced_degree_hours,
        "advanced_degree_units_available": round(advanced_units_available, 3),
        "delta_u_by_year": delta_u_by_year,
        "carry_remaining": round(carry, 2),
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
