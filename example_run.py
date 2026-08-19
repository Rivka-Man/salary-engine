"""
דוגמה מלאה: מורה 'רונית כהן' - מועסקת בתנאי עוז לתמורה משנת תשע"ג,
עם היסטוריית לימודים על פני כמה שנים, כולל שנת שבתון אחת.
"""
import sqlite3
import json
import os
from engine import init_db, compute_gimul_hishtalmut, DB_PATH

# 1. אתחול DB (יוצר את הטבלאות + זורע את טבלת החוקים)
init_db(reset=True)

conn = sqlite3.connect(DB_PATH)

# 2. הזנת נתוני המורה
conn.execute("""
    INSERT INTO teachers (teacher_id, full_name, is_active_teacher, has_seniority_year,
        is_pensioner, in_reform, reform_start_year, dual_reform_ofek_hadash,
        U_pre, H_pre, has_double_degree_gimul, has_beinayim_gimul)
    VALUES ('T001', 'רונית כהן', 1, 1, 0, 1, 'תשע"ג', 0, 16, 1900, 0, 0)
""")
# הסבר: U_pre=16, H_pre=1900 שעות (מעל 16*112=1792) --> אמורה לזכות ביחידת מעבר (R08)

# 3. הזנת קורסים לאורך השנים
courses = [
    # (start_date, hours, source_type, graded, grade, pre_approved, advanced_degree)
    ("2012-10-01", 60,  "academic",     1, 85, 1, 0),   # תשע"ג
    ("2013-11-15", 40,  "school_based", 0, None, 1, 0), # תשע"ד - השתלמות ביה"ס
    ("2013-12-01", 20,  "school_based", 0, None, 1, 0), # תשע"ד - עוד השתלמות ביה"ס (יחד עם הקודם = 60, יחתך ל-30)
    ("2015-09-10", 120, "academic",     1, 78, 1, 0),   # תשע"ו - שנת שבתון (ראה סטטוס למטה)
    ("2018-10-01", 90,  "academic",     1, 90, 0, 0),   # תשע"ט - בלי אישור מוקדם, ייפסל
    ("2019-10-01", 100, "academic",     1, 88, 1, 0),   # תש"פ
    ("2021-01-01", 150, "academic",     1, 92, 1, 1),   # תשפ"א - מסלול תואר מתקדם (M.A.)
]
for c in courses:
    conn.execute("""
        INSERT INTO courses (teacher_id, start_date, hours, source_type, graded, grade,
                              pre_approved, is_advanced_degree_track)
        VALUES ('T001', ?, ?, ?, ?, ?, ?, ?)
    """, c)

# 4. סטטוס תעסוקתי שנתי
statuses = [
    ('תשע"ג', 'רגיל'), ('תשע"ד', 'רגיל'), ('תשע"ו', 'שבתון'),
    ('תשע"ט', 'רגיל'), ('תש"פ', 'רגיל'), ('תשפ"א', 'רגיל'),
]
for sy, status in statuses:
    conn.execute("""
        INSERT INTO employment_status_by_year (teacher_id, school_year, status)
        VALUES ('T001', ?, ?)
    """, (sy, status))

conn.commit()
conn.close()

# 5. הרצת המנוע
result = compute_gimul_hishtalmut("T001", calc_date="2022-01-01")

print("=" * 70)
print("תוצאת החישוב עבור רונית כהן (T001), נכון ל-2022-01-01")
print("=" * 70)
print(f"יחידת גמול (H_unit):              {result['H_unit']} שעות")
print(f"תקרה שחלה (U_cap):                {result['U_cap_applied']} יח'")
print(f"בסיס לאחר יחידת מעבר:              {result['U_base_from_transition']} יח'")
print(f"יחידות שנצברו (לפני חיתוך תקרה):    {result['U_regular_earned_before_cap']} יח'")
print(f"יחידות רגילות סופיות (אחרי תקרה):   {result['U_regular']} יח'")
print(f"כפל תואר/ביניים:                   {result['U_dd_or_mid']} יח'")
print(f"סה\"כ יחידות (U_total):             {result['U_total']} יח'")
print(f"תוספת שכר (%):                    {result['pct_salary_addition']}%")
print(f"שעות תואר מתקדם שנצברו (בנפרד):     {result['advanced_degree_hours_accumulated']} שעות "
      f"(~{result['advanced_degree_units_available']} יח', גמול 1/שנה)")
print(f"קורסים שנפסלו:                    {result['rejected_courses']}")
print()
print("פירוט יחידות חדשות לפי שנה:")
for sy, delta in result["delta_u_by_year"].items():
    print(f"   {sy}: +{delta} יח'")
print()
print("=" * 70)
print("Trace מלא (audit trail) - כל חוק שהופעל ומה תרם:")
print("=" * 70)
for step in result["trace"]:
    val = f" -> {step['value']}" if step["value"] is not None else ""
    print(f"[{step['rule_id']}] {step['message']}{val}")

# 6. שמירת הפלט המלא כ-JSON לקובץ
output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "example_output.json")
with open(output_path, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print("\nהפלט המלא נשמר גם ל-example_output.json")
