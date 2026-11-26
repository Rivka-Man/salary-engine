"""
נתוני דמו להצגה: שרה כהן, תלוש ספטמבר 2026.

לא מוחק כלום מלבד המורה T900: מוסיף (או מעדכן) אותה ומדפיס את הקישור האישי שלה.
אפשר להריץ שוב ושוב.

    python demo_seed.py            # התרחיש העשיר (ברירת המחדל): שבתון + קורס שהוזן ידנית
    python demo_seed.py --classic  # התרחיש של התסריט המקורי: 9.75 = יחידת מעבר + כפל תואר

התרחיש העשיר:
  12 יחידות ערב הרפורמה (תשע"ב), גמול כפל תואר מ-2015 (8.75 יח', כבר בתלוש).
  תשפ"ב, תשפ"ג: קורס של 112 שעות בכל שנה -> יחידה בכל שנה (תקרה של יחידה לשנה).
  תשפ"ה: שנת שבתון. שלושה קורסים, 336 שעות -> 3 יחידות, כי בשבתון התקרה לא חלה (R12).
  תשפ"ו: קורס של 112 שעות שהמורה הזינה בעצמה (לא מופיע בפורטל) -> יחידה.
  מגיע: 12 + 1 + 1 + 3 + 1 + 8.75 = 26.75.  בתלוש: 22.75.  פער: 4 יחידות.
  הפער = השבתון (3) + הקורס הידני (1): חשבות לא עדכנה את תשפ"ה, והמוסד לא דיווח את תשפ"ו.
  -> סיבות A + B: פנייה לחשבות השכר ומייל למוסד ההשתלמות, במקביל.

אסור להריץ את example_run.py: הוא מוחק את כל הטבלאות במסד המשותף.
"""
import json
import secrets
import sys
from datetime import datetime

from db import get_connection
from engine import compute_gimul_hishtalmut, init_db, school_year_of

CLASSIC = "--classic" in sys.argv
TID = "T900"
PAY_MONTH = "2026-09"

init_db(reset=False)
conn = get_connection()
for t in ("payslips", "calculation_runs", "employment_status_by_year", "courses"):
    conn.execute(f"DELETE FROM {t} WHERE teacher_id=%s", (TID,))
conn.execute("DELETE FROM teacher_documents WHERE teacher_id=%s", (TID,))
conn.execute("DELETE FROM teachers WHERE teacher_id=%s", (TID,))
conn.execute("""
    INSERT INTO teachers (teacher_id, full_name, is_active_teacher, has_seniority_year, is_pensioner,
        in_reform, reform_start_year, reform_start_date, u_pre, h_pre, has_double_degree_gimul,
        double_degree_start_date, has_beinayim_gimul)
    VALUES (%s, 'שרה כהן', 1, 1, 0, 1, 'תשע"ב', '2011-09-01', %s, %s, 1, '2015-09-01', 0)
""", (TID, 16 if CLASSIC else 12, 1900 if CLASSIC else 0))

# (התחלה, סיום, תיאור, מוסד, שעות, מקור)
COURSES = [] if CLASSIC else [
    ("2021-10-03", "2022-05-30", "הוראה מותאמת לתלמידים עם לקויות למידה", "מכון מופ\"ת", 112, "portal"),
    ("2022-10-02", "2023-05-30", "הערכה חלופית בכיתה", "מכללת לוינסקי", 112, "portal"),
    ("2024-10-06", "2025-01-20", "בינה מלאכותית בהוראה", "מכון מופ\"ת", 112, "portal"),
    ("2025-01-26", "2025-04-28", "הוראת מתמטיקה בחטיבה העליונה", "האוניברסיטה העברית", 112, "portal"),
    ("2025-03-02", "2025-06-15", "חינוך מיוחד: הכלה בכיתה רגילה", "מכללת דוד ילין", 112, "portal"),
    ("2025-10-05", "2026-05-25", "למידה מבוססת פרויקטים", "מכללת אורנים", 112, "manual"),
]
for start, end, desc, inst, hours, src in COURSES:
    conn.execute("""
        INSERT INTO courses (teacher_id, start_date, end_date, description, school_year, hours,
            hours_graded, source_type, graded, grade, pre_approved, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'academic', 1, 92, 1, %s)
    """, (TID, start, end, f"{desc} — {inst}", school_year_of(start), hours, hours, src))
if not CLASSIC:
    for sy, status in (('תשפ"ב', "רגיל"), ('תשפ"ג', "רגיל"), ('תשפ"ה', "שבתון"), ('תשפ"ו', "רגיל")):
        conn.execute("INSERT INTO employment_status_by_year (teacher_id, school_year, status) VALUES (%s, %s, %s)",
                     (TID, sy, status))
conn.commit()
conn.close()

result = compute_gimul_hishtalmut(TID, f"{PAY_MONTH}-01")
expected = float(result["U_total"])
GAP = 9.75 if CLASSIC else 4.0
paid = round(expected - GAP, 2)
rate = result.get("pct_per_unit") or 1.3


def L(code, desc, qty=None, amount=None, frm=None, to=None, diff=None, employer=None, severance=None):
    return {"code": code, "description": desc, "qty_or_pct": qty, "amount": amount, "from_month": frm,
            "to_month": to, "diff": diff, "employer": employer, "severance": severance}


# המספרים כמו בהדמיה (T2): שורות השכר בבלוק עוז לתמורה
oz = [L("102", "שכר משולב עוז", None, 912.40), L("199", "תוספת עוז לתמורה", "42.00", 381.10),
      L("5614", "שקלית אחידה 2016 ע", None, 275.00), L("5832", "ת.שקלית עוז 23", None, 298.60)]
base = sum(l["amount"] for l in oz)
amount = round(base * paid * rate / 100, 2)
oz += [L("249", "ג. השת. עוז לתמורה", f"{paid:.2f}", amount),
       L("142", "תואר עוז לתמורה", "5.0%", 64.20), L("141", "הכשרה עוז לתמורה", "6.0%", 77.05)]
oz_total = round(sum(l["amount"] for l in oz), 2)
payments = oz + [L(None, "סה\"כ עוז לתמורה", None, oz_total),
                 L("53", "החזר טלפון", None, 43.40),
                 L("261", "קצובת נסיעה", None, None, "08/2026", "08/2026", 21.00),
                 L("263", "ק.נסיעה ח.חודשי", None, 310.00)]
gross = round(oz_total + 43.40 + 21.00 + 310.00, 2)
mandatory = [L(None, "מס הכנסה", None, None), L(None, "ביטוח לאומי", None, 198.20),
             L(None, "ביטוח בריאות", None, 212.70), L(None, "הפרשי ב. בריאות", None, 1.10)]
gemel = [L(None, "ק.השתלמות", "5,120.00", 128.00, employer=390.00),
         L(None, "קרן פנסיה", "5,180.00", 362.60, employer=466.20, severance=420.00)]
mand_total = round(198.20 + 212.70 + 1.10, 2)
gemel_total = round(128.00 + 362.60, 2)
extraction = {
    "is_payslip": True, "pay_month": PAY_MONTH,
    "gimul": {"code": "249", "description": "ג. השת. עוז לתמורה", "units": paid, "amount": amount},
    "oz_lines": oz,
    "sections": [
        {"title": "תשלומים", "kind": "payments", "lines": payments},
        {"title": "ניכויי חובה", "kind": "mandatory", "lines": mandatory},
        {"title": "ניכויים/הפרשות גמל", "kind": "gemel", "lines": gemel},
    ],
    "totals": {"gross": gross, "mandatory_deductions": mand_total, "gemel_deductions": gemel_total,
               "net": round(gross - mand_total - gemel_total, 2)},
    "notes": "",
}
conn = get_connection()
conn.execute("""
    INSERT INTO payslips (teacher_id, pay_month, gimul_code, gimul_desc, gimul_units, gimul_amount,
        extraction_json, created_at)
    VALUES (%s, %s, '249', 'ג. השת. עוז לתמורה', %s, %s, %s, %s)
""", (TID, PAY_MONTH, paid, amount, json.dumps(extraction, ensure_ascii=False),
      datetime.now().isoformat(timespec="seconds")))
conn.execute("UPDATE teachers SET access_token=%s WHERE teacher_id=%s AND access_token IS NULL",
             (secrets.token_urlsafe(16), TID))
token = conn.execute("SELECT access_token FROM teachers WHERE teacher_id=%s", (TID,)).fetchone()["access_token"]
conn.commit()
conn.close()
print(f"שרה כהן ({TID}, {'קלאסי' if CLASSIC else 'שבתון'}): מגיע {expected} יח', בתלוש {paid} יח', פער {GAP}")
print(f"קישור אישי: /my/{token}/payslip")
