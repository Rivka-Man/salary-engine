"""
המסלול הירוק לדמו: "קבצים" בלחיצה, בלי לגרור ובלי מפתח API.

כל קובץ דמו הוא כמה בתים שמתחילים ב-DEMO_MAGIC ואחריהם סוג הקובץ. הוא נשמר
ב-teacher_documents כמו כל העלאה, ו-files.detect מזהה אותו לפני הקריאות
האמיתיות ומחזיר את הנתונים של שרה כהן (אותם מספרים כמו demo_seed.py). משם
הכול רץ בדרך הרגילה: read_file שומר לטבלאות, case_status, המנוע, ההשוואה
והפניות. רק הקריאה של הקובץ מדומה.

    reset_demo(conn, "T900")   # שרה בנקודת ההתחלה: בלי תלוש, בלי קורסים מהפורטל, בלי שבתון
    make("payslip")            # -> (filename, bytes)
"""
from agent.store import reset_case

DEMO_MAGIC = b"OPTIPAY-DEMO:"
PAY_MONTH = "2026-09"
PAID_UNITS = 22.75  # בתלוש. מגיע 26.75 -> פער 4 (שבתון 3 + קורס שלא דווח 1)

FILES = {
    "payslip": "תלוש_09_2026.pdf",
    "courses": "הקורסים_שלי.pdf",
    "oz_join": "עוז_הצטרפות.pdf",
    "vacations": "חלת_ושבתון_תשפה.pdf",
    "unknown": "תעודת_הוקרה.pdf",
}

PORTAL_COURSES = [
    ("2021-10-03", "2022-05-30", "הוראה מותאמת לתלמידים עם לקויות למידה — מכון מופ\"ת"),
    ("2022-10-02", "2023-05-30", "הערכה חלופית בכיתה — מכללת לוינסקי"),
    ("2024-10-06", "2025-01-20", "בינה מלאכותית בהוראה — מכון מופ\"ת"),
    ("2025-01-26", "2025-04-28", "הוראת מתמטיקה בחטיבה העליונה — האוניברסיטה העברית"),
    ("2025-03-02", "2025-06-15", "חינוך מיוחד: הכלה בכיתה רגילה — מכללת דוד ילין"),
]
# הקורס שהמורה הזינה בעצמה, ולא מופיע בפורטל (סיבה B: המוסד לא דיווח)
MANUAL_COURSE = ("2025-10-05", "2026-05-25", "למידה מבוססת פרויקטים — מכללת אורנים", 'תשפ"ו')
VACATIONS = {'תשפ"ב': "רגיל", 'תשפ"ג': "רגיל", 'תשפ"ה': "שבתון", 'תשפ"ו': "רגיל"}


def make(kind):
    if kind not in FILES:
        raise ValueError(kind)
    return FILES[kind], DEMO_MAGIC + kind.encode()


def is_demo(data):
    return bytes(data[:len(DEMO_MAGIC)]) == DEMO_MAGIC


def _line(code, desc, qty=None, amount=None):
    return {"code": code, "description": desc, "qty_or_pct": qty, "amount": amount, "from_month": None,
            "to_month": None, "diff": None, "employer": None, "severance": None}


def _payslip():
    oz = [_line("102", "שכר משולב עוז", None, 912.40), _line("199", "תוספת עוז לתמורה", "42.00", 381.10),
          _line("5614", "שקלית אחידה 2016 ע", None, 275.00), _line("5832", "ת.שקלית עוז 23", None, 298.60)]
    base = sum(x["amount"] for x in oz)
    amount = round(base * PAID_UNITS * 1.3 / 100, 2)
    oz += [_line("249", "ג. השת. עוז לתמורה", f"{PAID_UNITS:.2f}", amount),
           _line("142", "תואר עוז לתמורה", "5.0%", 64.20), _line("141", "הכשרה עוז לתמורה", "6.0%", 77.05)]
    return {"is_payslip": True, "pay_month": PAY_MONTH,
            "gimul": {"code": "249", "description": "ג. השת. עוז לתמורה", "units": PAID_UNITS, "amount": amount},
            "oz_lines": oz, "sections": [{"title": "תשלומים", "kind": "payments", "lines": oz}],
            "totals": {}, "notes": "קובץ דמו"}


def parse(data):
    """(doc_type, parsed) לקובץ דמו, באותו מבנה שהקריאות האמיתיות מחזירות."""
    kind = bytes(data[len(DEMO_MAGIC):]).decode(errors="ignore").strip()
    if kind == "payslip":
        return "payslip", _payslip()
    if kind == "courses":
        return "courses", [{"start_date": s, "end_date": e, "name": n, "hours": 112, "hours_graded": 112,
                            "hours_ungraded": 0, "source_type_guess": "academic", "counts_for_oz": True}
                           for s, e, n in PORTAL_COURSES]
    if kind == "oz_join":
        return "oz_join", {"reform_start_date": "2011-09-01"}
    if kind == "vacations":
        return "vacations", dict(VACATIONS)
    return "unknown", None


def reset_demo(conn, tid="T900"):
    """
    המורה בנקודת ההתחלה של הדמו. נתוני הרקע (ותק, יחידות ערב הרפורמה, כפל תואר)
    נשארים כמו ב-demo_seed.py; מה שמגיע מהקבצים נמחק, כדי שהקבצים ימלאו אותו.
    """
    if not conn.execute("SELECT 1 FROM teachers WHERE teacher_id=%s", (tid,)).fetchone():
        raise LookupError(f"אין מורה {tid}. הריצי קודם: python demo_seed.py")
    for t in ("payslips", "calculation_runs", "employment_status_by_year", "teacher_documents"):
        conn.execute(f"DELETE FROM {t} WHERE teacher_id=%s", (tid,))
    conn.execute("DELETE FROM courses WHERE teacher_id=%s", (tid,))
    conn.execute("""UPDATE teachers SET is_active_teacher=1, has_seniority_year=1, is_pensioner=0, in_reform=1,
                    reform_start_year=NULL, reform_start_date=NULL, u_pre=12, h_pre=0,
                    has_double_degree_gimul=1, double_degree_start_date='2015-09-01', has_beinayim_gimul=0
                    WHERE teacher_id=%s""", (tid,))
    s, e, n, sy = MANUAL_COURSE
    conn.execute("""INSERT INTO courses (teacher_id, start_date, end_date, description, school_year, hours,
                    hours_graded, source_type, graded, grade, pre_approved, source)
                    VALUES (%s,%s,%s,%s,%s,112,112,'academic',1,92,1,'manual')""", (tid, s, e, n, sy))
    conn.commit()
    reset_case(conn, tid)
