"""
נתוני דמו להצגה: שרה כהן, תלוש ספטמבר 2026.

לא מוחק כלום: מוסיף (או מעדכן) את המורה T900 בלבד ומדפיס את הקישור האישי שלה.
    python demo_seed.py

הפער בתלוש: 9.75 יחידות = יחידת המעבר (R08, +1) + גמול כפל תואר (R04, +8.75).
שני הרכיבים מוכרים במשרד ולא הגיעו לתלוש -> סיבה A במאגר הפעולות.
"""
import json
import secrets
from datetime import datetime

from db import get_connection
from engine import compute_gimul_hishtalmut, init_db

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
    VALUES (%s, 'שרה כהן', 1, 1, 0, 1, 'תשע"ב', '2011-09-01', 16, 1900, 1, '2015-09-01', 0)
""", (TID,))
courses = [
    ("2016-10-02", "2017-05-30", "הוראת מתמטיקה בחטיבת הביניים", 112, "portal"),
    ("2019-10-06", "2020-06-10", "למידה מבוססת פרויקטים", 112, "portal"),
    ("2023-10-15", "2024-05-20", "הערכה מעצבת בכיתה", 112, "portal"),
]
for start, end, desc, hours, src in courses:
    conn.execute("""
        INSERT INTO courses (teacher_id, start_date, end_date, description, hours, hours_graded,
            source_type, graded, grade, pre_approved, is_advanced_degree_track, counts_for_oz, source)
        VALUES (%s, %s, %s, %s, %s, %s, 'institutional', 1, 90, 1, 0, 1, %s)
    """, (TID, start, end, desc, hours, hours, src))
conn.commit()
conn.close()

result = compute_gimul_hishtalmut(TID, f"{PAY_MONTH}-01")
expected = float(result["U_total"])
paid = round(expected - 9.75, 2)
rate = result.get("pct_per_unit") or 1.3
base = 4910.30  # השכר בחלק המשרה שבעוז לתמורה (שורות 102 + 199 + שקליות)
amount = round(base * paid * rate / 100, 2)

payments = [
    {"code": "102", "description": "שכר משולב עוז", "qty_or_pct": None, "amount": 3954.60},
    {"code": "199", "description": "תוספת עוז לתמורה", "qty_or_pct": "42.00", "amount": 381.10},
    {"code": "5614", "description": "שקלית אחידה 2016 ע", "qty_or_pct": None, "amount": 275.00},
    {"code": "5832", "description": "ת.שקלית עוז 23", "qty_or_pct": None, "amount": 298.60},
    {"code": "249", "description": "ג. השת. עוז לתמורה", "qty_or_pct": f"{paid:.2f}", "amount": amount},
    {"code": "142", "description": "תואר עוז לתמורה", "qty_or_pct": "5.0%", "amount": 245.52},
    {"code": "141", "description": "הכשרה עוז לתמורה", "qty_or_pct": "6.0%", "amount": 294.62},
]
oz_total = round(sum(l["amount"] for l in payments), 2)
payments.append({"code": None, "description": "סה\"כ עוז לתמורה", "qty_or_pct": None, "amount": oz_total})
payments += [
    {"code": "53", "description": "החזר טלפון", "qty_or_pct": None, "amount": 43.40},
    {"code": "263", "description": "ק.נסיעה ח.חודשי", "qty_or_pct": None, "amount": 310.00},
]
gross = round(oz_total + 43.40 + 310.00, 2)
extraction = {
    "is_payslip": True, "pay_month": PAY_MONTH,
    "gimul": {"code": "249", "description": "ג. השת. עוז לתמורה", "units": paid, "amount": amount},
    "oz_lines": [l for l in payments[:7]],
    "sections": [
        {"title": "תשלומים", "kind": "payments", "lines": payments},
        {"title": "ניכויי חובה", "kind": "mandatory", "lines": [
            {"code": None, "description": "מס הכנסה", "qty_or_pct": None, "amount": 612.40},
            {"code": None, "description": "ביטוח לאומי", "qty_or_pct": None, "amount": 198.20},
            {"code": None, "description": "ביטוח בריאות", "qty_or_pct": None, "amount": 212.70},
        ]},
        {"title": "ניכויים/הפרשות גמל", "kind": "gemel", "lines": [
            {"code": None, "description": "קרן השתלמות", "qty_or_pct": "5,120.00", "amount": 128.00},
            {"code": None, "description": "קרן פנסיה", "qty_or_pct": "5,180.00", "amount": 362.60},
        ]},
    ],
    "totals": {"gross": gross, "mandatory_deductions": 1023.30, "net": round(gross - 1023.30 - 490.60, 2)},
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
print(f"שרה כהן ({TID}): מגיע {expected} יח', בתלוש {paid} יח', פער 9.75")
print(f"קישור אישי: /my/{token}/payslip")
