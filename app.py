"""
ממשק ווב להזנת נתוני מורים והרצת מנוע גמול ההשתלמות.
מריץ מעל אותו PostgreSQL (Supabase) שמשתמש בו engine.py.
"""
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
from datetime import date, datetime, timedelta

import psycopg2
from flask import Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for

from agreements import bp as agreements_bp
from backoffice import bp as backoffice_bp
from db import get_connection
from engine import RULES_METADATA, SCHOOL_YEAR_START, compute_gimul_hishtalmut, init_db, school_year_of
from pdf_import import parse_courses_pdf, parse_maktav_haasaka_pdf, parse_oz_join_pdf
import payslips

app = Flask(__name__)
# בשרת: SECRET_KEY ממשתנה סביבה. בלעדיו - מפתח אקראי לכל הפעלה (התחברויות
# מתאפסות באתחול השרת, אבל אף פעם לא מפתח קבוע וידוע מהקוד).
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(hours=12)
# התחברות מנהל: נגמרת כשסוגרים את הדפדפן, וגם אחרי זמן בלי פעילות. נבדק בשרת
# (לא רק לפי עוגיית הדפדפן), כי דפדפנים ששומרים לשוניות שומרים גם עוגיות.
ADMIN_IDLE_TIMEOUT = timedelta(minutes=30)

# סיסמת ממשק הניהול. בלי ADMIN_PASSWORD ממשק הניהול נעול לגמרי, חוץ מהרצה
# מקומית (localhost) לפיתוח.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or ""

init_db(reset=False)


def _is_local_request():
    return request.host.split(":")[0] in ("localhost", "127.0.0.1") and request.remote_addr == "127.0.0.1"


@app.before_request
def require_admin_login():
    # הממשק העצמאי של המורה (/my/<token>) מוגן ע"י האסימון האישי, וקבצים
    # סטטיים פתוחים. כל השאר הוא ממשק ניהול.
    if request.endpoint in (None, "static", "admin_login", "admin_logout") or request.path.startswith("/my/"):
        return None
    if session.get("is_admin"):
        now = datetime.now().timestamp()
        last_seen = session.get("admin_last_seen")
        # התחברות ישנה בלי חותמת זמן (מלפני השינוי הזה) נחשבת פגה
        if last_seen and now - last_seen < ADMIN_IDLE_TIMEOUT.total_seconds():
            session["admin_last_seen"] = now
            return None
        session.clear()
        flash("פג תוקף ההתחברות. יש להיכנס שוב.", "error")
    elif not ADMIN_PASSWORD and _is_local_request():
        return None
    return redirect(url_for("admin_login", next=request.full_path if request.method == "GET" else None))


@app.route("/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password") or ""
        if ADMIN_PASSWORD and hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
            session.clear()
            session.permanent = False  # העוגייה נמחקת כשסוגרים את הדפדפן
            session["is_admin"] = True
            session["admin_last_seen"] = datetime.now().timestamp()
            nxt = request.args.get("next") or ""
            # רק נתיב פנימי - לא הפניה לאתר חיצוני
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("index")
            return redirect(nxt)
        flash("סיסמה שגויה." if ADMIN_PASSWORD else "לא הוגדרה סיסמת ניהול בשרת (ADMIN_PASSWORD).", "error")
    return render_template("login.html", hide_admin_nav=True)


@app.route("/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))

app.register_blueprint(backoffice_bp)
app.register_blueprint(agreements_bp)


def _rule_texts():
    """
    כותרות ותיאורי החוקים מה-DB (נערכים בבקאופיס) - נטען פעם אחת לכל בקשה.
    חוק שחסר ב-DB נופל חזרה לטקסט שבקוד (RULES_METADATA).
    """
    if "rule_texts" not in g:
        texts = {r["rule_id"]: (r["title"], r["description"]) for r in RULES_METADATA}
        conn = get_connection()
        for r in conn.execute("SELECT rule_id, title, description FROM rules").fetchall():
            texts[r["rule_id"]] = (r["title"], r["description"])
        conn.close()
        g.rule_texts = texts
    return g.rule_texts


@app.template_filter("rule_desc")
def rule_desc(rule_id):
    """מחליף rule_id (כמו R09_THRESH_18) בכותרת המילולית של החוק, לתצוגה במורה."""
    return _rule_texts().get(rule_id, (rule_id, None))[0]


@app.template_filter("rule_tooltip")
def rule_tooltip(rule_id):
    """טקסט הסבר מלא ל-tooltip (title HTML attribute): קוד החוק + התיאור המלא."""
    desc = _rule_texts().get(rule_id, (None, None))[1]
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
    "school_based": "השתלמות בית ספרית",
    "institutional": 'השתלמות מוסדית (פיסג"ה)',
}

SCHOOL_YEARS = list(SCHOOL_YEAR_START.keys())


def get_conn():
    return get_connection()


def get_teacher_by_token(conn, token):
    if not token:
        return None
    return conn.execute("SELECT * FROM teachers WHERE access_token=%s", (token,)).fetchone()


DOC_TYPE_LABELS = {
    "maktav": "מכתב העסקה",
    "oz_join": "הצטרפות לעוז לתמורה",
    "heikef": "היקף משרה",  # קבצים ישנים, לפני שהוחלף בעמוד הצטרפות לעוז לתמורה
    "courses": "הקורסים שלי",
    "payslip": "תלוש שכר",
}


def save_document(conn, teacher_id, doc_type, filename, content):
    """שומר עותק של קובץ שנטען (גם אם הפענוח שלו נכשל) - לתיעוד ולבדיקה חוזרת."""
    conn.execute(
        "INSERT INTO teacher_documents (teacher_id, doc_type, filename, content, uploaded_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (teacher_id, doc_type, filename, psycopg2.Binary(content), datetime.now().isoformat(timespec="seconds")),
    )


def list_documents(conn, teacher_id):
    return conn.execute(
        "SELECT doc_id, doc_type, filename, uploaded_at, length(content) AS size FROM teacher_documents "
        "WHERE teacher_id=%s ORDER BY uploaded_at DESC, doc_id DESC",
        (teacher_id,),
    ).fetchall()


def document_response(conn, teacher_id, doc_id):
    doc = conn.execute(
        "SELECT filename, content FROM teacher_documents WHERE doc_id=%s AND teacher_id=%s",
        (doc_id, teacher_id),
    ).fetchone()
    if not doc:
        return None
    mimetype = mimetypes.guess_type(doc["filename"])[0] or "application/pdf"
    return send_file(io.BytesIO(bytes(doc["content"])), mimetype=mimetype,
                     as_attachment=False, download_name=doc["filename"])


def delete_document(conn, teacher_id, doc_id):
    """מוחק רק את עותק הקובץ מהרשימה - נתונים שכבר נקלטו ממנו נשארים. מחזיר אם נמחק."""
    cur = conn.execute(
        "DELETE FROM teacher_documents WHERE doc_id=%s AND teacher_id=%s",
        (doc_id, teacher_id),
    )
    conn.commit()
    return cur.rowcount > 0


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
    documents = list_documents(conn, teacher_id)
    teacher_payslips = conn.execute(
        "SELECT * FROM payslips WHERE teacher_id=%s ORDER BY pay_month DESC, payslip_id DESC", (teacher_id,)
    ).fetchall()
    conn.close()
    return render_template(
        "teacher_detail.html",
        documents=documents,
        payslips=teacher_payslips,
        month_label=payslips.month_label,
        doc_type_labels=DOC_TYPE_LABELS,
        teacher=teacher,
        courses=courses,
        statuses=statuses,
        runs=runs,
        school_years=SCHOOL_YEARS,
        source_type_labels=SOURCE_TYPE_LABELS,
        today=date.today().isoformat(),
    )


@app.route("/teachers/<teacher_id>/documents/<int:doc_id>")
def teacher_document(teacher_id, doc_id):
    conn = get_conn()
    resp = document_response(conn, teacher_id, doc_id)
    conn.close()
    return resp or abort(404)


@app.route("/teachers/<teacher_id>/documents/<int:doc_id>/delete", methods=["POST"])
def teacher_document_delete(teacher_id, doc_id):
    conn = get_conn()
    deleted = delete_document(conn, teacher_id, doc_id)
    conn.close()
    if not deleted:
        abort(404)
    flash("הקובץ נמחק מהרשימה.", "success")
    return redirect(url_for("teacher_detail", teacher_id=teacher_id))


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
            request.form.get("source_type", "institutional"),
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

    data = uploaded.read()
    conn = get_conn()
    save_document(conn, teacher["teacher_id"], "courses", uploaded.filename, data)
    conn.commit()
    conn.close()
    try:
        rows = parse_courses_pdf(io.BytesIO(data))
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
             source_type, graded, grade, pre_approved, is_advanced_degree_track, counts_for_oz)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 0, NULL, 1, 0, %s)
            """,
            (
                teacher_id,
                start_date,
                sy,
                float(hours),
                float(hg) if hg else None,
                float(hu) if hu else None,
                request.form.get(f"source_type_{i}", "institutional"),
                0 if request.form.get(f"counts_for_oz_{i}") == "0" else 1,
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
    conn.execute("DELETE FROM teacher_documents WHERE teacher_id=%s", (teacher_id,))
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
        certification_date = request.form.get("certification_date") or None
        conn.execute(
            """
            UPDATE teachers SET
                is_active_teacher=%s, has_seniority_year=%s, is_pensioner=%s,
                in_reform=1, reform_start_date=%s, reform_start_year=%s,
                certification_date=%s, u_pre=%s, h_pre=%s
            WHERE teacher_id=%s
            """,
            (
                # אין יותר תיבת סימון נפרדת ל"עובד הוראה מוסמך" - השורה
                # אוחדה לשדה תאריך הסמכה יחיד; קיום תאריך = מוסמך.
                1 if certification_date else 0,
                1 if request.form.get("has_seniority_year") else 0,
                1 if request.form.get("is_pensioner") else 0,
                reform_start_date,
                school_year_of(reform_start_date) if reform_start_date else None,
                certification_date,
                int(float(request.form.get("u_pre") or 0)),
                int(float(request.form.get("h_pre") or 0)),
                teacher["teacher_id"],
            ),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("my_wizard_education", token=token))

    documents = list_documents(conn, teacher["teacher_id"])
    conn.close()
    return render_template(
        "my_step_employment.html", token=token, teacher=teacher,
        school_years=SCHOOL_YEARS, journey_step=0,
        documents=documents, doc_type_labels=DOC_TYPE_LABELS,
    )


@app.route("/my/<token>/wizard/docs", methods=["POST"])
def my_wizard_docs_upload(token):
    """
    טעינה מרוכזת של שלושת קובצי הפורטל בשלב 1: מכתב העסקה, הצטרפות לעוז לתמורה,
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

    f = request.files.get("file_oz_join")
    if f and f.filename:
        data = f.read()
        save_document(conn, tid, "oz_join", f.filename, data)
        try:
            found = parse_oz_join_pdf(io.BytesIO(data))
            if found.get("reform_start_date"):
                d = found["reform_start_date"]
                conn.execute(
                    "UPDATE teachers SET reform_start_date=%s, reform_start_year=%s WHERE teacher_id=%s",
                    (d, school_year_of(d), tid),
                )
                ok.append(f"הצטרפות לעוז לתמורה: תאריך משרה ראשונה בעוז לתמורה — {d}")
            else:
                errs.append("הצטרפות לעוז לתמורה: לא זוהה תאריך. ודאי שזה קובץ \"עוז לתמורה - הצטרפות ויציאה\" מהפורטל.")
        except Exception as exc:
            errs.append(f"הצטרפות לעוז לתמורה: לא הצלחתי לקרוא את הקובץ ({exc}).")

    f = request.files.get("file_maktav")
    if f and f.filename:
        data = f.read()
        save_document(conn, tid, "maktav", f.filename, data)
        try:
            found = parse_maktav_haasaka_pdf(io.BytesIO(data))
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
        data = f.read()
        save_document(conn, tid, "courses", f.filename, data)
        try:
            rows = parse_courses_pdf(io.BytesIO(data))
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
                         source_type, graded, grade, pre_approved, is_advanced_degree_track, counts_for_oz)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 0, NULL, 1, 0, %s)
                        """,
                        (tid, r["start_date"], sy, float(r["hours"]),
                         r.get("hours_graded"), r.get("hours_ungraded"),
                         r.get("source_type_guess", "institutional"),
                         1 if r.get("counts_for_oz", True) else 0),
                    )
                not_oz = sum(1 for r in rows if not r.get("counts_for_oz", True))
                note = f", מהם {not_oz} שלא נספרים לעוז לתמורה (רק בטרום הרפורמה)" if not_oz else ""
                ok.append(f"הקורסים שלי: נטענו {len(rows)} קורסים{note} (מחליפים את הרשימה הקודמת).")
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


@app.route("/my/<token>/documents/<int:doc_id>")
def my_document(token, doc_id):
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    resp = document_response(conn, teacher["teacher_id"], doc_id) if teacher else None
    conn.close()
    return resp or abort(404)


@app.route("/my/<token>/documents/<int:doc_id>/delete", methods=["POST"])
def my_document_delete(token, doc_id):
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    deleted = delete_document(conn, teacher["teacher_id"], doc_id) if teacher else False
    conn.close()
    if not deleted:
        abort(404)
    flash("הקובץ נמחק מהרשימה.", "success")
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
        # אין יותר תיבות סימון נפרדות - כל הכרה מסומנת "יש" ע"י מילוי
        # תאריך "החל מ-" בלבד; שדה ריק = אין הכרה.
        double_degree_start_date = request.form.get("double_degree_start_date") or None
        additional_double_degree_start_date = request.form.get("additional_double_degree_start_date") or None
        beinayim_start_date = request.form.get("beinayim_start_date") or None
        conn.execute(
            """
            UPDATE teachers SET
                has_double_degree_gimul=%s, double_degree_start_date=%s,
                has_additional_double_degree_gimul=%s, additional_double_degree_start_date=%s,
                has_beinayim_gimul=%s, beinayim_start_date=%s
            WHERE teacher_id=%s
            """,
            (
                1 if double_degree_start_date else 0,
                double_degree_start_date,
                1 if additional_double_degree_start_date else 0,
                additional_double_degree_start_date,
                1 if beinayim_start_date else 0,
                beinayim_start_date,
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
            request.form.get("source_type", "institutional"),
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

    data = uploaded.read()
    conn = get_conn()
    save_document(conn, teacher["teacher_id"], "courses", uploaded.filename, data)
    conn.commit()
    conn.close()
    try:
        rows = parse_courses_pdf(io.BytesIO(data))
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
             pre_approved, is_advanced_degree_track, counts_for_oz)
            VALUES (%s, %s, %s, %s, %s, 0, NULL, 1, 0, %s)
            """,
            (
                teacher["teacher_id"],
                start_date,
                sy,
                float(hours),
                request.form.get(f"source_type_{i}", "institutional"),
                0 if request.form.get(f"counts_for_oz_{i}") == "0" else 1,
            ),
        )
        added += 1
    conn.commit()
    conn.close()
    flash(f"נוספו {added} קורסים מהקובץ.", "success")
    return redirect(url_for("my_wizard_courses", token=token))


# ==================================================================
# "התלוש שלי": טעינת תלוש, התלוש מוסבר, פער אחד והפעולה לתיקון
# ==================================================================

PAYSLIP_FORM_FIELDS = ("pay_month", "gimul_code", "gimul_desc", "gimul_units", "gimul_amount")


def _run_for_month(teacher_id, pay_month):
    """מריץ את החישוב לחודש התלוש ומחזיר את run_id שנשמר."""
    compute_gimul_hishtalmut(teacher_id, f"{pay_month}-01")
    conn = get_conn()
    row = conn.execute(
        "SELECT max(run_id) AS run_id FROM calculation_runs WHERE teacher_id=%s", (teacher_id,)
    ).fetchone()
    conn.close()
    return row["run_id"]


@app.route("/my/<token>/payslip")
def my_payslip(token):
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    if not teacher:
        conn.close()
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))
    tid = teacher["teacher_id"]
    slip_id = request.args.get("id", type=int)
    if slip_id:
        slip = conn.execute("SELECT * FROM payslips WHERE payslip_id=%s AND teacher_id=%s", (slip_id, tid)).fetchone()
    else:
        slip = conn.execute(
            "SELECT * FROM payslips WHERE teacher_id=%s ORDER BY pay_month DESC, payslip_id DESC LIMIT 1", (tid,)
        ).fetchone()
    if not slip:
        conn.close()
        return redirect(url_for("my_payslip_new", token=token))
    all_slips = conn.execute(
        "SELECT payslip_id, pay_month FROM payslips WHERE teacher_id=%s ORDER BY pay_month DESC, payslip_id DESC", (tid,)
    ).fetchall()
    run = conn.execute(
        "SELECT result_json FROM calculation_runs WHERE run_id=%s AND teacher_id=%s", (slip["run_id"], tid)
    ).fetchone() if slip["run_id"] else None
    result = json.loads(run["result_json"]) if run else None
    cmp = payslips.compare(conn, result, slip) if result else None
    conn.close()
    return render_template(
        "my_payslip.html", token=token, teacher=teacher, slip=slip, result=result, cmp=cmp,
        letter=payslips.letter(cmp, slip, teacher["full_name"]) if cmp else "",
        all_slips=all_slips, month_label=payslips.month_label,
    )


@app.route("/my/<token>/payslip/new")
def my_payslip_new(token):
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    conn.close()
    if not teacher:
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))
    return render_template("my_payslip_upload.html", token=token, teacher=teacher)


def _render_payslip_confirm(token, teacher, form, extracted=None, error=None, doc_id=None, filename=None):
    return render_template(
        "my_payslip_confirm.html", token=token, teacher=teacher, form=form, extracted=extracted,
        error=error, doc_id=doc_id, filename=filename,
        extraction_json=json.dumps(extracted, ensure_ascii=False) if extracted else "",
    )


@app.route("/my/<token>/payslip/upload", methods=["POST"])
def my_payslip_upload(token):
    """טוען את קובץ התלוש, שומר עותק ומנסה לחלץ את שורת הגמול. מציג טופס לאישור/תיקון.
    בלי קובץ (או כשהקריאה נכשלת) - אותו טופס ריק להזנה ידנית."""
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    if not teacher:
        conn.close()
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))
    extracted, error, doc_id, filename = None, None, None, None
    uploaded = request.files.get("payslip_file")
    if uploaded and uploaded.filename:
        data = uploaded.read()
        filename = uploaded.filename
        try:
            payslips.file_block(filename, data)  # בדיקת סוג וגודל לפני שמירה
            save_document(conn, teacher["teacher_id"], "payslip", filename, data)
            doc_id = conn.execute(
                "SELECT max(doc_id) AS d FROM teacher_documents WHERE teacher_id=%s", (teacher["teacher_id"],)
            ).fetchone()["d"]
            conn.commit()
            extracted = payslips.extract_payslip(filename, data)
        except payslips.PayslipError as exc:
            error = str(exc)
        except Exception as exc:  # לא מפילים את המסך - עוברים להזנה ידנית
            app.logger.exception("payslip extraction failed")
            error = f"לא הצלחתי לקרוא את התלוש ({exc.__class__.__name__}). אפשר להזין את השורה ידנית."
    conn.close()
    gimul = (extracted or {}).get("gimul") or {}
    form = {
        "pay_month": (extracted or {}).get("pay_month") or "",
        "gimul_code": gimul.get("code") or payslips.GIMUL_CODE,
        "gimul_desc": gimul.get("description") or "ג. השת. עוז לתמורה",
        "gimul_units": "" if gimul.get("units") is None else gimul["units"],
        "gimul_amount": "" if gimul.get("amount") is None else gimul["amount"],
    }
    return _render_payslip_confirm(token, teacher, form, extracted, error, doc_id, filename)


@app.route("/my/<token>/payslip/confirm", methods=["POST"])
def my_payslip_confirm(token):
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    if not teacher:
        conn.close()
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))
    f = request.form
    pay_month = (f.get("pay_month") or "").strip()
    doc_id = f.get("doc_id", type=int)

    def number(name):
        raw = (f.get(name) or "").replace(",", "").replace("%", "").replace("₪", "").strip()
        return float(raw) if raw else None

    try:
        units, amount = number("gimul_units"), number("gimul_amount")
        valid = payslips.valid_month(pay_month) and (units is None or 0 <= units <= 60)
    except ValueError:
        valid = False
    if not valid:
        conn.close()
        extracted = None
        if f.get("extraction_json"):
            try:
                extracted = json.loads(f["extraction_json"])
            except ValueError:
                extracted = None
        return _render_payslip_confirm(
            token, teacher, {k: f.get(k, "") for k in PAYSLIP_FORM_FIELDS}, extracted,
            "יש לבחור את חודש התלוש ולהזין מספר יחידות תקין (למשל 9.75).", doc_id, f.get("filename"),
        )
    if doc_id and not conn.execute(
        "SELECT 1 FROM teacher_documents WHERE doc_id=%s AND teacher_id=%s", (doc_id, teacher["teacher_id"])
    ).fetchone():
        doc_id = None
    conn.close()
    try:
        run_id = _run_for_month(teacher["teacher_id"], pay_month)
    except Exception as exc:
        flash(f"שגיאה בהרצת החישוב: {exc}", "error")
        return redirect(url_for("my_payslip_new", token=token))
    conn = get_conn()
    row = conn.execute(
        """INSERT INTO payslips (teacher_id, pay_month, gimul_code, gimul_desc, gimul_units, gimul_amount,
                                 doc_id, extraction_json, run_id, created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING payslip_id""",
        (teacher["teacher_id"], pay_month, (f.get("gimul_code") or "").strip() or None,
         (f.get("gimul_desc") or "").strip() or None, units, amount, doc_id,
         f.get("extraction_json") or None, run_id, datetime.now().isoformat(timespec="seconds")),
    ).fetchone()
    conn.commit()
    conn.close()
    return redirect(url_for("my_payslip", token=token, id=row["payslip_id"]))


@app.route("/my/<token>/payslip/<int:payslip_id>/recalc", methods=["POST"])
def my_payslip_recalc(token, payslip_id):
    """מחשב מחדש מול הנתונים הנוכחיים (למשל אחרי שהמורה הוסיפה קורסים)."""
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    slip = conn.execute(
        "SELECT * FROM payslips WHERE payslip_id=%s AND teacher_id=%s", (payslip_id, teacher["teacher_id"])
    ).fetchone() if teacher else None
    conn.close()
    if not teacher:
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))
    if not slip:
        return redirect(url_for("my_payslip", token=token))
    try:
        run_id = _run_for_month(teacher["teacher_id"], slip["pay_month"])
    except Exception as exc:
        flash(f"שגיאה בהרצת החישוב: {exc}", "error")
        return redirect(url_for("my_payslip", token=token, id=payslip_id))
    conn = get_conn()
    conn.execute("UPDATE payslips SET run_id=%s WHERE payslip_id=%s", (run_id, payslip_id))
    conn.commit()
    conn.close()
    flash("החישוב עודכן לפי הנתונים הנוכחיים.", "success")
    return redirect(url_for("my_payslip", token=token, id=payslip_id))


@app.route("/my/<token>/payslip/<int:payslip_id>/delete", methods=["POST"])
def my_payslip_delete(token, payslip_id):
    conn = get_conn()
    teacher = get_teacher_by_token(conn, token)
    if teacher:
        conn.execute("DELETE FROM payslips WHERE payslip_id=%s AND teacher_id=%s", (payslip_id, teacher["teacher_id"]))
        conn.commit()
    conn.close()
    if not teacher:
        flash("קישור לא תקין.", "error")
        return redirect(url_for("index"))
    flash("התלוש הוסר.", "success")
    return redirect(url_for("my_payslip", token=token))


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
