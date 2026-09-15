"""
ממשק ווב להזנת נתוני מורים והרצת מנוע גמול ההשתלמות.
מריץ מעל אותו PostgreSQL (Supabase) שמשתמש בו engine.py.
"""
import io
import json
import re
import secrets
from datetime import date

from flask import Flask, flash, redirect, render_template, request, url_for

from db import get_connection
from engine import RULES_METADATA, SCHOOL_YEAR_START, compute_gimul_hishtalmut, init_db, school_year_of
from pdf_import import parse_courses_pdf, parse_heikef_misra_pdf, parse_maktav_haasaka_pdf

app = Flask(__name__)
app.secret_key = "salary-engine-local-dev"

init_db(reset=False)

RULE_TITLES = {r["rule_id"]: r["title"] for r in RULES_METADATA}
RULE_DESCRIPTIONS = {r["rule_id"]: r["description"] for r in RULES_METADATA}


@app.template_filter("rule_desc")
def rule_desc(rule_id):
    """מחליף rule_id (כמו R09_THRESH_18) בכותרת המילולית של החוק, לתצוגה במורה."""
    return RULE_TITLES.get(rule_id, rule_id)


@app.template_filter("rule_tooltip")
def rule_tooltip(rule_id):
    """טקסט הסבר מלא ל-tooltip (title HTML attribute): קוד החוק + התיאור המלא."""
    desc = RULE_DESCRIPTIONS.get(rule_id)
    return f"{rule_id} — {desc}" if desc else rule_id


@app.template_filter("num")
def format_number(value):
    """
    מציג מספר (מחושב או לא) עם לכל היותר 2 ספרות אחרי הנקודה - לא נוגע
    בערך המאוחסן/מחושב בפועל, רק בתצוגה. מספר שלם מוצג בלי נקודה כלל.
    ערכים שאינם מספריים (None, טקסט) עוברים ללא שינוי.
    """
    if value is None:
        return value
    try:
        rounded = round(float(value), 2)
    except (TypeError, ValueError):
        return value
    return int(rounded) if rounded == int(rounded) else rounded


@app.template_filter("dmy")
def format_date_dmy(value):
    """תאריך ISO (YYYY-MM-DD) -> DD/MM/YYYY לתצוגה. ערך ריק/לא-תאריך עובר כמו שהוא."""
    if not value:
        return value
    s = str(value)[:10]
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else value


SOURCE_TYPE_LABELS = {
    "academic": "אקדמי",
    "school_based": "השתלמות בית ספרית",
    "conference": "כנס",
    "language": "שפה",
    "other": "אחר",
}

SCHOOL_YEARS = list(SCHOOL_YEAR_START.keys())


def get_conn():
    return get_connection()


def get_teacher_by_token(conn, token):
    if not token:
        return None
    return conn.execute("SELECT * FROM teachers WHERE access_token=%s", (token,)).fetchone()


def next_teacher_id(conn):
    rows = conn.execute("SELECT teacher_id FROM teachers").fetchall()
    max_n = 0
    for r in rows:
        m = re.match(r"^T(\d+)$", r["teacher_id"])
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"T{max_n + 1:03d}"


def parse_course_hours(form):
    """
    טופס הוספת קורס נותן שני שדות נפרדים - שעות עם ציון / בלי ציון
    (כמו בקובץ הפורטל) - במקום שדה שעות בודד + checkbox. מחזיר
    (hours, graded) או (None, None) אם אף אחד מהשניים לא מולא.
    """
    hg = form.get("hours_graded")
    hu = form.get("hours_ungraded")
    if hg:
        return float(hg), 1
    if hu:
        return float(hu), 0
    return None, None


@app.route("/")
def index():
    conn = get_conn()
    teachers = conn.execute("SELECT * FROM teachers ORDER BY full_name").fetchall()
    conn.close()
    return render_template("index.html", teachers=teachers)


@app.route("/teachers/new", methods=["GET", "POST"])
def new_teacher():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        if not full_name:
            flash("יש למלא שם מלא.", "error")
            return render_template("new_teacher.html", school_years=SCHOOL_YEARS, form=request.form)
        conn = get_conn()
        teacher_id = next_teacher_id(conn)
        conn.execute(
            """
            INSERT INTO teachers
            (teacher_id, full_name, is_active_teacher, has_seniority_year, is_pensioner,
             in_reform, reform_start_year, u_pre, h_pre,
             has_double_degree_gimul, has_beinayim_gimul, access_token)
            VALUES (%s, %s, 1, 1, 0, 1, NULL, 0, 0, 0, 0, %s)
            """,
            (teacher_id, full_name, secrets.token_urlsafe(16)),
        )
        conn.commit()
        conn.close()
        flash(f"המורה {full_name} נוצר/ה בהצלחה. אפשר להעתיק את הקישור האישי ולשלוח אותו, או להשלים פרטים נוספים.", "success")
        return redirect(url_for("teacher_detail", teacher_id=teacher_id))

    return render_template("new_teacher.html", school_years=SCHOOL_YEARS, form=None)


@app.route("/teachers/<teacher_id>")
def teacher_detail(teacher_id):
    conn = get_conn()
    teacher = conn.execute("SELECT * FROM teachers WHERE teacher_id=%s", (teacher_id,)).fetchone()
    if not teacher:
        conn.close()
        flash("מורה לא נמצא.", "error")
        return redirect(url_for("index"))
    courses = conn.execute(
        "SELECT * FROM courses WHERE teacher_id=%s ORDER BY start_date", (teacher_id,)
    ).fetchall()
    statuses = conn.execute(
        "SELECT * FROM employment_status_by_year WHERE teacher_id=%s", (teacher_id,)
    ).fetchall()
    statuses = sorted(statuses, key=lambda s: SCHOOL_YEAR_START.get(s["school_year"], s["school_year"]))
    runs = conn.execute(
        "SELECT * FROM calculation_runs WHERE teacher_id=%s ORDER BY run_timestamp DESC", (teacher_id,)
    ).fetchall()
    conn.close()
    return render_template(
        "teacher_detail.html",
        teacher=teacher,
        courses=courses,
        statuses=statuses,
        runs=runs,
        school_years=SCHOOL_YEARS,
        source_type_labels=SOURCE_TYPE_LABELS,
        today=date.today().isoformat(),
    )


@app.route("/teachers/<teacher_id>/edit", methods=["GET", "POST"])
def edit_teacher(teacher_id):
    conn = get_conn()
    teacher = conn.execute("SELECT * FROM teachers WHERE teacher_id=%s", (teacher_id,)).fetchone()
    if not teacher:
        conn.close()
        flash("מורה לא נמצא.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        if not full_name:
            conn.close()
            flash("יש למלא שם מלא.", "error")
            return redirect(url_for("edit_teacher", teacher_id=teacher_id))
        conn.execute(
            """
            UPDATE teachers SET
                full_name=%s, is_active_teacher=%s, has_seniority_year=%s, is_pensioner=%s,
                in_reform=%s, reform_start_year=%s, u_pre=%s, h_pre=%s,
                has_double_degree_gimul=%s, has_beinayim_gimul=%s, has_additional_double_degree_gimul=%s
            WHERE teacher_id=%s
            """,
            (
                full_name,
                1 if request.form.get("is_active_teacher") else 0,
                1 if request.form.get("has_seniority_year") else 0,
                1 if request.form.get("is_pensioner") else 0,
                1 if request.form.get("in_reform") else 0,
                request.form.get("reform_start_year") or None,
                float(request.form.get("U_pre") or 0),
                float(request.form.get("H_pre") or 0),
                1 if request.form.get("has_double_degree_gimul") else 0,
                1 if request.form.get("has_beinayim_gimul") else 0,
                1 if request.form.get("has_additional_double_degree_gimul") else 0,
                teacher_id,
            ),
        )
        conn.commit()
        conn.close()
        flash("הפרופיל עודכן.", "success")
        return redirect(url_for("teacher_detail", teacher_id=teacher_id))

    conn.close()
    return render_template("edit_teacher.html", teacher=teacher, school_years=SCHOOL_YEARS)


@app.route("/teachers/<teacher_id>/courses", methods=["POST"])
def add_course(teacher_id):
    start_date = request.form.get("start_date")
    hours, graded = parse_course_hours(request.form)
    if not start_date or hours is None:
        flash("יש למלא תאריך תחילה ושעות (עם ציון או בלי ציון).", "error")
        return redirect(url_for("teacher_detail", teacher_id=teacher_id))
    try:
        sy = school_year_of(start_date)
    except Exception:
        sy = None
    grade = request.form.get("grade") or None
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO courses
        (teacher_id, start_date, end_date, description, school_year, hours, source_type, graded, grade,
         pre_approved, is_advanced_degree_track)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            teacher_id,
            start_date,
            request.form.get("end_date") or None,
            request.form.get("description") or None,
            sy,
            hours,
            request.form.get("source_type", "academic"),
            graded,
            float(grade) if (graded and grade) else None,
            1 if request.form.get("pre_approved") else 0,
            1 if request.form.get("is_advanced_degree_track") else 0,
        ),
    )
    conn.commit()
    conn.close()
    flash("הקורס נוסף.", "success")
    return redirect(url_for("teacher_detail", teacher_id=teacher_id))


@app.route("/teachers/<teacher_id>/import-pdf", methods=["GET"])
def import_courses_form(teacher_id):
    conn = get_conn()
    teacher = conn.execute("SELECT * FROM teachers WHERE teacher_id=%s", (teacher_id,)).fetchone()
    conn.close()
    if not teacher:
        flash("מורה לא נמצא.", "error")
        return redirect(url_for("index"))
    return render_template(
        "import_courses.html",
        teacher=teacher,
        form_action=url_for("import_courses_parse", teacher_id=teacher_id),
        cancel_url=url_for("teacher_detail", teacher_id=teacher_id),
    )


@app.route("/teachers/<teacher_id>/import-pdf", methods=["POST"])
def import_courses_parse(teacher_id):
    conn = get_conn()
    teacher = conn.execute("SELECT * FROM teachers WHERE teacher_id=%s", (teacher_id,)).fetchone()
    conn.close()
    if not teacher:
        flash("מורה לא נמצא.", "error")
        return redirect(url_for("index"))

    uploaded = request.files.get("pdf_file")
    if not uploaded or not uploaded.filename:
        flash("יש לבחור קובץ PDF.", "error")
        return redirect(url_for("import_courses_form", teacher_id=teacher_id))
    if not uploaded.filename.lower().endswith(".pdf"):
        flash("הקובץ חייב להיות PDF.", "error")
        return redirect(url_for("import_courses_form", teacher_id=teacher_id))

    try:
        rows = parse_courses_pdf(io.BytesIO(uploaded.read()))
    except Exception as exc:
        flash(f"לא הצלחתי לקרוא את הקובץ: {exc}", "error")
        return redirect(url_for("import_courses_form", teacher_id=teacher_id))

    if not rows:
        flash("לא זוהו קורסים בקובץ. ודאי שזה קובץ PDF שהופק מ\"הקורסים שלי\" בפורטל עו\"ה.", "error")
        return redirect(url_for("import_courses_form", teacher_id=teacher_id))

    earliest_school_year = None
    if not teacher["reform_start_year"]:
        try:
            earliest_school_year = min(
                (school_year_of(r["start_date"]) for r in rows),
                key=lambda sy: SCHOOL_YEAR_START.get(sy, "9999"),
            )
        except Exception:
            earliest_school_year = None

    return render_template(
        "import_preview.html",
        teacher=teacher,
        rows=rows,
        source_type_labels=SOURCE_TYPE_LABELS,
        earliest_school_year=earliest_school_year,
        form_action=url_for("import_courses_confirm", teacher_id=teacher_id),
        cancel_url=url_for("teacher_detail", teacher_id=teacher_id),
        edit_profile_url=url_for("edit_teacher", teacher_id=teacher_id),
    )


@app.route("/teachers/<teacher_id>/import-pdf/confirm", methods=["POST"])
def import_courses_confirm(teacher_id):
    count = int(request.form.get("row_count", 0))
    conn = get_conn()
    added = 0
    for i in range(count):
        if not request.form.get(f"include_{i}"):
            continue
        start_date = request.form.get(f"start_date_{i}")
        hours = request.form.get(f"hours_{i}")
        if not start_date or not hours:
            continue
        try:
            sy = school_year_of(start_date)
        except Exception:
            sy = None
        hg = request.form.get(f"hours_graded_{i}") or None
        hu = request.form.get(f"hours_ungraded_{i}") or None
        conn.execute(
            """
            INSERT INTO courses
            (teacher_id, start_date, school_year, hours, hours_graded, hours_ungraded,
             source_type, graded, grade, pre_approved, is_advanced_degree_track)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 0, NULL, 1, 0)
            """,
            (
                teacher_id,
                start_date,
                sy,
                float(hours),
                float(hg) if hg else None,
                float(hu) if hu else None,
                request.form.get(f"source_type_{i}", "other"),
            ),
        )
        added += 1
    conn.commit()
    conn.close()
    flash(f"נוספו {added} קורסים מהקובץ.", "success")
    return redirect(url_for("teacher_detail", teacher_id=teacher_id))


@app.route("/teachers/<teacher_id>/courses/<int:course_id>/delete", methods=["POST"])
def delete_course(teacher_id, course_id):
    conn = get_conn()
    conn.execute("DELETE FROM courses WHERE course_id=%s AND teacher_id=%s", (course_id, teacher_id))
    conn.commit()
    conn.close()
    return redirect(url_for("teacher_detail", teacher_id=teacher_id))


@app.route("/teachers/<teacher_id>/status", methods=["POST"])
def add_status(teacher_id):
    school_year = request.form.get("school_year")
    status = request.form.get("status", "רגיל")
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO employment_status_by_year (teacher_id, school_year, status)
        VALUES (%s, %s, %s)
        ON CONFLICT(teacher_id, school_year) DO UPDATE SET status=excluded.status
        """,
        (teacher_id, school_year, status),
    )
    conn.commit()
    conn.close()
    flash("סטטוס תעסוקתי עודכן.", "success")
    return redirect(url_for("teacher_detail", teacher_id=teacher_id))


@app.route("/teachers/<teacher_id>/status/<school_year>/delete", methods=["POST"])
def delete_status(teacher_id, school_year):
    conn = get_conn()
    conn.execute(
        "DELETE FROM employment_status_by_year WHERE teacher_id=%s AND school_year=%s",
        (teacher_id, school_year),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("teacher_detail", teacher_id=teacher_id))


@app.route("/teachers/<teacher_id>/delete", methods=["POST"])
def delete_teacher(teacher_id):
    conn = get_conn()
    conn.execute("DELETE FROM courses WHERE teacher_id=%s", (teacher_id,))
    conn.execute("DELETE FROM employment_status_by_year WHERE teacher_id=%s", (teacher_id,))
    conn.execute("DELETE FROM calculation_runs WHERE teacher_id=%s", (teacher_id,))
    conn.execute("DELETE FROM teachers WHERE teacher_id=%s", (teacher_id,))
    conn.commit()
    conn.close()
    flash("המורה נמחק.", "success")
    return redirect(url_for("index"))


@app.route("/teachers/<teacher_id>/calculate", methods=["POST"])
def calculate(teacher_id):
    calc_date = request.form.get("calc_date") or date.today().isoformat()
    try:
        result = compute_gimul_hishtalmut(teacher_id, calc_date)
    except Exception as exc:
        flash(f"שגיאה בהרצת החישוב: {exc}", "error")
        return redirect(url_for("teacher_detail", teacher_id=teacher_id))
    conn = get_conn()
    teacher = conn.execute("SELECT * FROM teachers WHERE teacher_id=%s", (teacher_id,)).fetchone()
    conn.close()
    return render_template("result.html", teacher=teacher, result=result)


@app.route("/runs/<int:run_id>")
def view_run(run_id):
    conn = get_conn()
    run = conn.execute("SELECT * FROM calculation_runs WHERE run_id=%s", (run_id,)).fetchone()
    if not run:
        conn.close()
        flash("הרצה לא נמצאה.", "error")
        return redirect(url_for("index"))
    teacher = conn.execute(
        "SELECT * FROM teachers WHERE teacher_id=%s", (run["teacher_id"],)
    ).fetchone()
    conn.close()
    result = json.loads(run["result_json"])
    return render_template("result.html", teacher=teacher, result=result)


@app.route("/teachers/<teacher_id>/regenerate-link", methods=["POST"])
def regenerate_link(teacher_id):
    conn = get_conn()
    conn.execute(
        "UPDATE teachers SET access_token=%s WHERE teacher_id=%s",
        (secrets.token_urlsafe(16), teacher_id),
    )
    conn.commit()
    conn.close()
    flash("הקישור האישי חודש — הקישור הקודם כבר לא עובד.", "success")
    return redirect(url_for("teacher_detail", teacher_id=teacher_id))


# ==================================================================
# ממשק עצמאי למורה: קישור אישי, בלי login. אשף מודרך צעד-אחר-צעד
# (העסקה/וותק ← השכלה ← קורסים ← סיכום), בנוסף לקיצורי דרך לעדכון
# סטטוס תעסוקתי ולהוספת קורס בודד. לא מאפשר מחיקת מורה ולא צפייה
# במורות אחרות - המורה יכולה לערוך רק את הפרופיל/נתונים של עצמה.
# ==================================================================

@app.route("/my/<token>")
def my_dashboard(token):
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    conn.close()
    if not teacher:
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))
    return render_template("my_intro.html", token=token, teacher=teacher, journey_step=-1)


@app.route("/my/<token>/wizard/employment", methods=["GET", "POST"])
def my_wizard_employment(token):
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    if not teacher:
        conn.close()
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        # הקישור האישי מיועד אך ורק למורות/מורים בעוז לתמורה (הכותרת/המיתוג
        # של האשף כולו הוא "גמולי השתלמות עוז לתמורה") - אין יותר תיבת
        # סימון "מועסקת בתנאי עוז לתמורה"; in_reform קבוע כאן ל-1.
        reform_start_date = request.form.get("reform_start_date") or None
        conn.execute(
            """
            UPDATE teachers SET
                is_active_teacher=%s, has_seniority_year=%s, is_pensioner=%s,
                in_reform=1, reform_start_date=%s, reform_start_year=%s,
                certification_date=%s, u_pre=%s, h_pre=%s
            WHERE teacher_id=%s
            """,
            (
                1 if request.form.get("is_active_teacher") else 0,
                1 if request.form.get("has_seniority_year") else 0,
                1 if request.form.get("is_pensioner") else 0,
                reform_start_date,
                school_year_of(reform_start_date) if reform_start_date else None,
                request.form.get("certification_date") or None,
                int(float(request.form.get("u_pre") or 0)),
                int(float(request.form.get("h_pre") or 0)),
                teacher["teacher_id"],
            ),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("my_wizard_education", token=token))

    conn.close()
    return render_template(
        "my_step_employment.html", token=token, teacher=teacher,
        school_years=SCHOOL_YEARS, journey_step=0,
    )


@app.route("/my/<token>/wizard/docs", methods=["POST"])
def my_wizard_docs_upload(token):
    """
    טעינה מרוכזת של שלושת קובצי הפורטל בשלב 1: מכתב העסקה, היקף משרה,
    הקורסים שלי. כל קובץ עובר לפרסר המתאים לו והנתונים נשמרים; מה שלא
    זוהה נשאר להזנה ידנית בהמשך.
    """
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    if not teacher:
        conn.close()
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))
    tid = teacher["teacher_id"]
    ok, errs = [], []

    f = request.files.get("file_heikef")
    if f and f.filename:
        try:
            found = parse_heikef_misra_pdf(io.BytesIO(f.read()))
            if found.get("reform_start_date"):
                d = found["reform_start_date"]
                conn.execute(
                    "UPDATE teachers SET reform_start_date=%s, reform_start_year=%s WHERE teacher_id=%s",
                    (d, school_year_of(d), tid),
                )
                ok.append(f"היקף משרה: תאריך הצטרפות לעוז לתמורה — {d}")
            else:
                errs.append("היקף משרה: לא זוהה תאריך הצטרפות. ודאי שזה קובץ \"היקף משרה\" מהפורטל.")
        except Exception as exc:
            errs.append(f"היקף משרה: לא הצלחתי לקרוא את הקובץ ({exc}).")

    f = request.files.get("file_maktav")
    if f and f.filename:
        try:
            found = parse_maktav_haasaka_pdf(io.BytesIO(f.read()))
            sets, params = [], []
            if found.get("certification_date"):
                sets.append("certification_date=%s"); params.append(found["certification_date"])
                ok.append(f"מכתב העסקה: תאריך הסמכה — {found['certification_date']}")
            for key, col, label in (
                ("double_degree_start_date", "double_degree_start_date", "כפל תואר"),
                ("additional_double_degree_start_date", "additional_double_degree_start_date", "כפל תואר נוסף"),
                ("beinayim_start_date", "beinayim_start_date", "גמול ביניים"),
            ):
                if found.get(key):
                    flag = {"double_degree_start_date": "has_double_degree_gimul",
                            "additional_double_degree_start_date": "has_additional_double_degree_gimul",
                            "beinayim_start_date": "has_beinayim_gimul"}[col]
                    sets.append(f"{col}=%s"); params.append(found[key])
                    sets.append(f"{flag}=1")
                    ok.append(f"מכתב העסקה: {label} — החל מ-{found[key]}")
            if sets:
                params.append(tid)
                conn.execute(f"UPDATE teachers SET {', '.join(sets)} WHERE teacher_id=%s", tuple(params))
            if not found:
                errs.append("מכתב העסקה: לא זוהו נתונים. ודאי שזה קובץ \"מכתב נתוני העסקה\" מהפורטל.")
        except Exception as exc:
            errs.append(f"מכתב העסקה: לא הצלחתי לקרוא את הקובץ ({exc}).")

    f = request.files.get("file_courses")
    if f and f.filename:
        try:
            rows = parse_courses_pdf(io.BytesIO(f.read()))
            if rows:
                conn.execute("DELETE FROM courses WHERE teacher_id=%s", (tid,))
                for r in rows:
                    try:
                        sy = school_year_of(r["start_date"])
                    except Exception:
                        sy = None
                    conn.execute(
                        """
                        INSERT INTO courses
                        (teacher_id, start_date, school_year, hours, hours_graded, hours_ungraded,
                         source_type, graded, grade, pre_approved, is_advanced_degree_track)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 0, NULL, 1, 0)
                        """,
                        (tid, r["start_date"], sy, float(r["hours"]),
                         r.get("hours_graded"), r.get("hours_ungraded"),
                         r.get("source_type_guess", "other")),
                    )
                ok.append(f"הקורסים שלי: נטענו {len(rows)} קורסים (מחליפים את הרשימה הקודמת).")
            else:
                errs.append("הקורסים שלי: לא זוהו קורסים. ודאי שזה קובץ \"הקורסים שלי\" מהפורטל.")
        except Exception as exc:
            errs.append(f"הקורסים שלי: לא הצלחתי לקרוא את הקובץ ({exc}).")

    conn.commit()
    conn.close()
    if not ok and not errs:
        flash("לא נבחר אף קובץ.", "error")
    if ok:
        flash("נטען בהצלחה — " + " · ".join(ok), "success")
    for e in errs:
        flash(e, "error")
    return redirect(url_for("my_wizard_employment", token=token))


@app.route("/my/<token>/wizard/education", methods=["GET", "POST"])
def my_wizard_education(token):
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    if not teacher:
        conn.close()
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        conn.execute(
            """
            UPDATE teachers SET
                has_double_degree_gimul=%s, double_degree_start_date=%s,
                has_additional_double_degree_gimul=%s, additional_double_degree_start_date=%s,
                has_beinayim_gimul=%s, beinayim_start_date=%s
            WHERE teacher_id=%s
            """,
            (
                1 if request.form.get("has_double_degree_gimul") else 0,
                request.form.get("double_degree_start_date") or None,
                1 if request.form.get("has_additional_double_degree_gimul") else 0,
                request.form.get("additional_double_degree_start_date") or None,
                1 if request.form.get("has_beinayim_gimul") else 0,
                request.form.get("beinayim_start_date") or None,
                teacher["teacher_id"],
            ),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("my_wizard_courses", token=token))

    conn.close()
    return render_template("my_step_education.html", token=token, teacher=teacher, journey_step=1)


@app.route("/my/<token>/wizard/courses", methods=["GET"])
def my_wizard_courses(token):
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    if not teacher:
        conn.close()
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))
    courses = conn.execute(
        "SELECT * FROM courses WHERE teacher_id=%s ORDER BY start_date", (teacher["teacher_id"],)
    ).fetchall()
    conn.close()
    return render_template(
        "my_step_courses.html", token=token, teacher=teacher, courses=courses,
        source_type_labels=SOURCE_TYPE_LABELS, journey_step=2,
    )


@app.route("/my/<token>/wizard/periods", methods=["GET"])
def my_wizard_periods(token):
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    if not teacher:
        conn.close()
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))
    statuses = conn.execute(
        "SELECT * FROM employment_status_by_year WHERE teacher_id=%s", (teacher["teacher_id"],)
    ).fetchall()
    statuses = sorted(statuses, key=lambda s: SCHOOL_YEAR_START.get(s["school_year"], s["school_year"]))
    conn.close()
    return render_template(
        "my_step_periods.html", token=token, teacher=teacher, statuses=statuses,
        school_years=SCHOOL_YEARS, journey_step=3,
    )


@app.route("/my/<token>/wizard/summary", methods=["POST"])
def my_wizard_summary(token):
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    conn.close()
    if not teacher:
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))
    calc_date = request.form.get("calc_date") or date.today().isoformat()
    try:
        result = compute_gimul_hishtalmut(teacher["teacher_id"], calc_date)
    except Exception as exc:
        flash(f"שגיאה בהרצת החישוב: {exc}", "error")
        return redirect(url_for("my_wizard_periods", token=token))
    return render_template(
        "my_summary.html", token=token, teacher=teacher, result=result, journey_step=4,
    )


@app.route("/my/<token>/runs/<int:run_id>")
def my_view_run(token, run_id):
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    if not teacher:
        conn.close()
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))
    run = conn.execute(
        "SELECT * FROM calculation_runs WHERE run_id=%s AND teacher_id=%s",
        (run_id, teacher["teacher_id"]),
    ).fetchone()
    conn.close()
    if not run:
        flash("הרצה לא נמצאה.", "error")
        return redirect(url_for("my_dashboard", token=token))
    result = json.loads(run["result_json"])
    return render_template("my_summary.html", token=token, teacher=teacher, result=result, journey_step=4)


@app.route("/my/<token>/courses", methods=["POST"])
def my_add_course(token):
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    if not teacher:
        conn.close()
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))
    start_date = request.form.get("start_date")
    hg = float(request.form.get("hours_graded") or 0)
    hu = float(request.form.get("hours_ungraded") or 0)
    total = hg + hu
    if not start_date or total <= 0:
        conn.close()
        flash("יש למלא תאריך תחילה ושעות (עם ציון או בלי ציון).", "error")
        return redirect(url_for("my_wizard_courses", token=token))
    try:
        sy = school_year_of(start_date)
    except Exception:
        sy = None
    grade = request.form.get("grade") or None
    conn.execute(
        """
        INSERT INTO courses
        (teacher_id, start_date, end_date, description, school_year, hours, hours_graded, hours_ungraded,
         source_type, graded, grade, pre_approved, is_advanced_degree_track)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            teacher["teacher_id"],
            start_date,
            request.form.get("end_date") or None,
            request.form.get("description") or None,
            sy,
            total,
            hg or None,
            hu or None,
            request.form.get("source_type", "academic"),
            1 if hg > 0 else 0,
            float(grade) if (hg > 0 and grade) else None,
            1 if request.form.get("pre_approved") else 0,
            1 if request.form.get("is_advanced_degree_track") else 0,
        ),
    )
    conn.commit()
    conn.close()
    flash("הקורס נוסף.", "success")
    return redirect(url_for("my_wizard_courses", token=token))


@app.route("/my/<token>/courses/<int:course_id>/delete", methods=["POST"])
def my_delete_course(token, course_id):
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    if not teacher:
        conn.close()
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))
    conn.execute(
        "DELETE FROM courses WHERE course_id=%s AND teacher_id=%s",
        (course_id, teacher["teacher_id"]),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("my_wizard_courses", token=token))


@app.route("/my/<token>/status", methods=["POST"])
def my_add_status(token):
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    if not teacher:
        conn.close()
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))
    school_year = request.form.get("school_year")
    status = request.form.get("status", "רגיל")
    conn.execute(
        """
        INSERT INTO employment_status_by_year (teacher_id, school_year, status)
        VALUES (%s, %s, %s)
        ON CONFLICT(teacher_id, school_year) DO UPDATE SET status=excluded.status
        """,
        (teacher["teacher_id"], school_year, status),
    )
    conn.commit()
    conn.close()
    flash("סטטוס תעסוקתי עודכן.", "success")
    return redirect(url_for("my_wizard_periods", token=token))


@app.route("/my/<token>/status/<school_year>/delete", methods=["POST"])
def my_delete_status(token, school_year):
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    if not teacher:
        conn.close()
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))
    conn.execute(
        "DELETE FROM employment_status_by_year WHERE teacher_id=%s AND school_year=%s",
        (teacher["teacher_id"], school_year),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("my_wizard_periods", token=token))


@app.route("/my/<token>/import-pdf", methods=["GET"])
def my_import_form(token):
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    conn.close()
    if not teacher:
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))
    return redirect(url_for("my_wizard_courses", token=token))


@app.route("/my/<token>/import-pdf", methods=["POST"])
def my_import_parse(token):
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    conn.close()
    if not teacher:
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))

    uploaded = request.files.get("pdf_file")
    if not uploaded or not uploaded.filename:
        flash("יש לבחור קובץ PDF.", "error")
        return redirect(url_for("my_wizard_courses", token=token))
    if not uploaded.filename.lower().endswith(".pdf"):
        flash("הקובץ חייב להיות PDF.", "error")
        return redirect(url_for("my_wizard_courses", token=token))

    try:
        rows = parse_courses_pdf(io.BytesIO(uploaded.read()))
    except Exception as exc:
        flash(f"לא הצלחתי לקרוא את הקובץ: {exc}", "error")
        return redirect(url_for("my_wizard_courses", token=token))

    if not rows:
        flash("לא זוהו קורסים בקובץ. ודאי שזה קובץ PDF שהופק מ\"הקורסים שלי\" בפורטל עו\"ה.", "error")
        return redirect(url_for("my_wizard_courses", token=token))

    earliest_school_year = None
    if not teacher["reform_start_year"]:
        try:
            earliest_school_year = min(
                (school_year_of(r["start_date"]) for r in rows),
                key=lambda sy: SCHOOL_YEAR_START.get(sy, "9999"),
            )
        except Exception:
            earliest_school_year = None

    return render_template(
        "my_import_preview.html",
        token=token,
        teacher=teacher,
        rows=rows,
        source_type_labels=SOURCE_TYPE_LABELS,
        earliest_school_year=earliest_school_year,
        form_action=url_for("my_import_confirm", token=token),
        cancel_url=url_for("my_wizard_courses", token=token),
        journey_step=2,
    )


@app.route("/my/<token>/import-pdf/confirm", methods=["POST"])
def my_import_confirm(token):
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    if not teacher:
        conn.close()
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))

    count = int(request.form.get("row_count", 0))
    added = 0
    for i in range(count):
        if not request.form.get(f"include_{i}"):
            continue
        start_date = request.form.get(f"start_date_{i}")
        hours = request.form.get(f"hours_{i}")
        if not start_date or not hours:
            continue
        try:
            sy = school_year_of(start_date)
        except Exception:
            sy = None
        conn.execute(
            """
            INSERT INTO courses
            (teacher_id, start_date, school_year, hours, source_type, graded, grade,
             pre_approved, is_advanced_degree_track)
            VALUES (%s, %s, %s, %s, %s, 0, NULL, 1, 0)
            """,
            (
                teacher["teacher_id"],
                start_date,
                sy,
                float(hours),
                request.form.get(f"source_type_{i}", "other"),
            ),
        )
        added += 1
    conn.commit()
    conn.close()
    flash(f"נוספו {added} קורסים מהקובץ.", "success")
    return redirect(url_for("my_wizard_courses", token=token))


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
