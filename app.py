"""
ממשק ווב מקומי להזנת נתוני מורים והרצת מנוע גמול ההשתלמות.
מריץ מעל אותו salary.db שמשתמש בו engine.py.
"""
import io
import json
import re
import sqlite3
from datetime import date

from flask import Flask, flash, redirect, render_template, request, url_for

from engine import DB_PATH, SCHOOL_YEAR_START, compute_gimul_hishtalmut, init_db, school_year_of
from pdf_import import parse_courses_pdf

app = Flask(__name__)
app.secret_key = "salary-engine-local-dev"

init_db(reset=False)

SOURCE_TYPE_LABELS = {
    "academic": "אקדמי",
    "school_based": "השתלמות בית ספרית",
    "conference": "כנס",
    "language": "שפה",
    "other": "אחר",
}

SCHOOL_YEARS = list(SCHOOL_YEAR_START.keys())


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def next_teacher_id(conn):
    rows = conn.execute("SELECT teacher_id FROM teachers").fetchall()
    max_n = 0
    for r in rows:
        m = re.match(r"^T(\d+)$", r["teacher_id"])
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"T{max_n + 1:03d}"


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
             in_reform, reform_start_year, U_pre, H_pre,
             has_double_degree_gimul, has_beinayim_gimul)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                teacher_id,
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
            ),
        )
        conn.commit()
        conn.close()
        flash(f"המורה {full_name} נוצר בהצלחה.", "success")
        return redirect(url_for("teacher_detail", teacher_id=teacher_id))

    return render_template("new_teacher.html", school_years=SCHOOL_YEARS, form=None)


@app.route("/teachers/<teacher_id>")
def teacher_detail(teacher_id):
    conn = get_conn()
    teacher = conn.execute("SELECT * FROM teachers WHERE teacher_id=?", (teacher_id,)).fetchone()
    if not teacher:
        conn.close()
        flash("מורה לא נמצא.", "error")
        return redirect(url_for("index"))
    courses = conn.execute(
        "SELECT * FROM courses WHERE teacher_id=? ORDER BY start_date", (teacher_id,)
    ).fetchall()
    statuses = conn.execute(
        "SELECT * FROM employment_status_by_year WHERE teacher_id=?", (teacher_id,)
    ).fetchall()
    statuses = sorted(statuses, key=lambda s: SCHOOL_YEAR_START.get(s["school_year"], s["school_year"]))
    runs = conn.execute(
        "SELECT * FROM calculation_runs WHERE teacher_id=? ORDER BY run_timestamp DESC", (teacher_id,)
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
    teacher = conn.execute("SELECT * FROM teachers WHERE teacher_id=?", (teacher_id,)).fetchone()
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
                full_name=?, is_active_teacher=?, has_seniority_year=?, is_pensioner=?,
                in_reform=?, reform_start_year=?, U_pre=?, H_pre=?,
                has_double_degree_gimul=?, has_beinayim_gimul=?
            WHERE teacher_id=?
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
    hours = request.form.get("hours")
    if not start_date or not hours:
        flash("יש למלא תאריך תחילה ושעות.", "error")
        return redirect(url_for("teacher_detail", teacher_id=teacher_id))
    try:
        sy = school_year_of(start_date)
    except Exception:
        sy = None
    graded = 1 if request.form.get("graded") else 0
    grade = request.form.get("grade") or None
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO courses
        (teacher_id, start_date, school_year, hours, source_type, graded, grade,
         pre_approved, is_advanced_degree_track)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            teacher_id,
            start_date,
            sy,
            float(hours),
            request.form.get("source_type", "academic"),
            graded,
            float(grade) if grade else None,
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
    teacher = conn.execute("SELECT * FROM teachers WHERE teacher_id=?", (teacher_id,)).fetchone()
    conn.close()
    if not teacher:
        flash("מורה לא נמצא.", "error")
        return redirect(url_for("index"))
    return render_template("import_courses.html", teacher=teacher)


@app.route("/teachers/<teacher_id>/import-pdf", methods=["POST"])
def import_courses_parse(teacher_id):
    conn = get_conn()
    teacher = conn.execute("SELECT * FROM teachers WHERE teacher_id=?", (teacher_id,)).fetchone()
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
        conn.execute(
            """
            INSERT INTO courses
            (teacher_id, start_date, school_year, hours, source_type, graded, grade,
             pre_approved, is_advanced_degree_track)
            VALUES (?, ?, ?, ?, ?, 0, NULL, 1, 0)
            """,
            (
                teacher_id,
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
    return redirect(url_for("teacher_detail", teacher_id=teacher_id))


@app.route("/teachers/<teacher_id>/courses/<int:course_id>/delete", methods=["POST"])
def delete_course(teacher_id, course_id):
    conn = get_conn()
    conn.execute("DELETE FROM courses WHERE course_id=? AND teacher_id=?", (course_id, teacher_id))
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
        VALUES (?, ?, ?)
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
        "DELETE FROM employment_status_by_year WHERE teacher_id=? AND school_year=?",
        (teacher_id, school_year),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("teacher_detail", teacher_id=teacher_id))


@app.route("/teachers/<teacher_id>/delete", methods=["POST"])
def delete_teacher(teacher_id):
    conn = get_conn()
    conn.execute("DELETE FROM courses WHERE teacher_id=?", (teacher_id,))
    conn.execute("DELETE FROM employment_status_by_year WHERE teacher_id=?", (teacher_id,))
    conn.execute("DELETE FROM calculation_runs WHERE teacher_id=?", (teacher_id,))
    conn.execute("DELETE FROM teachers WHERE teacher_id=?", (teacher_id,))
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
    teacher = conn.execute("SELECT * FROM teachers WHERE teacher_id=?", (teacher_id,)).fetchone()
    conn.close()
    return render_template("result.html", teacher=teacher, result=result)


@app.route("/runs/<int:run_id>")
def view_run(run_id):
    conn = get_conn()
    run = conn.execute("SELECT * FROM calculation_runs WHERE run_id=?", (run_id,)).fetchone()
    if not run:
        conn.close()
        flash("הרצה לא נמצאה.", "error")
        return redirect(url_for("index"))
    teacher = conn.execute(
        "SELECT * FROM teachers WHERE teacher_id=?", (run["teacher_id"],)
    ).fetchone()
    conn.close()
    result = json.loads(run["result_json"])
    return render_template("result.html", teacher=teacher, result=result)


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
