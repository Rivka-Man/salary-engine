"""
case_status: מה ידוע על המורה, מאיזה מקור, ומה עוד חסר כדי לענות.

"מה חסר" מסודר לפי מה שהכי ישנה את התשובה, והדרך הקלה ביותר להשיג אותו:
שאלת כן/לא קודם, קובץ רק אם אין ברירה (כללי האפיון).
"""
import payslips
from engine import SCHOOL_YEAR_START, get_courses, get_status_by_year, get_teacher, school_year_of

from agent.store import DOC_LABELS, portal_guide

H_UNIT = 112  # שעות ליחידה (R01). רק כדי להחליט אם לשאול על שבתון, לא לחישוב.

YEAR_STATUS_OPTIONS = ["כן, שבתון", "כן, חצי שבתון", "כן, חל\"ת", "לא"]


def _year_order(sy):
    keys = list(SCHOOL_YEAR_START.keys())
    return keys.index(sy) if sy in keys else -1


def _latest_payslip(conn, tid):
    return conn.execute(
        "SELECT * FROM payslips WHERE teacher_id=%s ORDER BY pay_month DESC, payslip_id DESC LIMIT 1", (tid,)
    ).fetchone()


def _doc_types(conn, tid):
    return {r["doc_type"] for r in conn.execute(
        "SELECT DISTINCT doc_type FROM teacher_documents WHERE teacher_id=%s", (tid,)).fetchall()}


def case_status(conn, teacher_id, case):
    """
    הכלי case_status. מחזיר:
    {"docs": [...], "known": [...], "missing": [...], "ready_to_check": bool, "next": {...} | None}
    כל פריט ב-missing: {"kind": "file" | "question", "key", "why", ...}
    """
    t = get_teacher(conn, teacher_id)
    courses = get_courses(conn, teacher_id)
    status = get_status_by_year(conn, teacher_id)
    slip = _latest_payslip(conn, teacher_id)
    types = _doc_types(conn, teacher_id)
    answered = {f["key"]: f for f in case.get("facts") or []}

    docs, known, missing = [], [], []

    # --- תלוש ---
    if slip:
        units = slip["gimul_units"]
        detail = f"{payslips.month_label(slip['pay_month'])} · " + (
            f"{payslips._fmt(units)} יח'" if units is not None else "אין שורה 249")
        docs.append({"doc_type": "payslip", "label": DOC_LABELS["payslip"], "status": "read", "detail": detail})
        known.append({"key": "paid_units", "label": "יחידות בתלוש", "value": units, "source": "payslip"})
    else:
        docs.append({"doc_type": "payslip", "label": DOC_LABELS["payslip"], "status": "missing", "detail": ""})
        missing.append({"kind": "file", "key": "payslip", "doc_type": "payslip", "priority": 1,
                        "why": "בלי תלוש אין למה להשוות."})

    # --- הצטרפות לעוז ---
    if t.get("reform_start_year"):
        docs.append({"doc_type": "oz_join", "label": DOC_LABELS["oz_join"], "status": "read",
                     "detail": t.get("reform_start_date") or t["reform_start_year"]})
        known.append({"key": "reform_start", "label": "הצטרפות לעוז", "value": t.get("reform_start_date") or t["reform_start_year"],
                      "source": "portal" if "oz_join" in types else "manual"})
    else:
        docs.append({"doc_type": "oz_join", "label": DOC_LABELS["oz_join"], "status": "missing", "detail": ""})
        missing.append({"kind": "file", "key": "oz_join", "doc_type": "oz_join", "priority": 2,
                        "why": "בלי תאריך ההצטרפות לא יודעים מאיזו שנה נספרות ההשתלמויות."})

    # --- הקורסים שלי ---
    portal_courses = [c for c in courses if c.get("source") != "manual"]
    manual_courses = [c for c in courses if c.get("source") == "manual"]
    if portal_courses or "courses" in types:
        docs.append({"doc_type": "courses", "label": DOC_LABELS["courses"], "status": "read",
                     "detail": f"{len(portal_courses)} קורסים"})
    else:
        docs.append({"doc_type": "courses", "label": DOC_LABELS["courses"], "status": "missing", "detail": ""})
        missing.append({"kind": "file", "key": "courses", "doc_type": "courses", "priority": 3,
                        "why": "ממנו מחשבים כמה יחידות מגיעות מההשתלמויות."})
    for c in manual_courses:
        known.append({"key": f"course:{c['course_id']}", "label": f"קורס {c.get('school_year') or ''}, {payslips._fmt(c.get('hours'))} שעות".strip(),
                      "value": c.get("description"), "source": "manual"})

    # --- כפל תואר / ביניים: מהמכתב, מהמשרד, או שאלה ---
    if t.get("has_double_degree_gimul") or t.get("has_beinayim_gimul") or "maktav" in types or "double_degree" in answered:
        label = "כפל תואר" if t.get("has_double_degree_gimul") else ("גמול ביניים" if t.get("has_beinayim_gimul") else "בלי כפל תואר")
        known.append({"key": "double_degree", "label": "כפל תואר / ביניים", "value": label,
                      "source": answered.get("double_degree", {}).get("source") or ("portal" if "maktav" in types else "office")})
    else:
        missing.append({"kind": "question", "key": "double_degree", "field": "double_degree", "priority": 5,
                        "text": "מוכר לך גמול כפל תואר או גמול ביניים?",
                        "options": ["כן, כפל תואר", "כן, גמול ביניים", "לא", "לא יודעת"],
                        "why": "כפל תואר שווה 8.75 יחידות. אם לא בטוחה, מכתב נתוני העסקה יגיד."})

    # --- שבתון / חל"ת: רק בשנה שבה למדה יותר מיחידה, אחרי ההצטרפות, והסטטוס לא ידוע ---
    hours_by_year = {}
    for c in courses:
        sy = c.get("school_year")
        if not sy and c.get("start_date"):
            try:
                sy = school_year_of(c["start_date"])
            except Exception:
                sy = None
        if sy:
            hours_by_year[sy] = hours_by_year.get(sy, 0) + (c.get("hours") or 0)
    start = t.get("reform_start_year")
    for sy, h in sorted(hours_by_year.items(), key=lambda x: _year_order(x[0])):
        if h <= H_UNIT or (start and _year_order(sy) < _year_order(start)):
            continue
        key = f"sabbatical:{sy}"
        if sy in status or key in answered:
            st = answered.get(key, {}).get("value") or status.get(sy)
            known.append({"key": key, "label": f"סטטוס {sy}", "value": st,
                          "source": answered.get(key, {}).get("source") or "manual"})
            continue
        missing.append({"kind": "question", "key": key, "field": "sabbatical", "school_year": sy, "priority": 4,
                        "text": f"היית בשבתון או בחל\"ת ב{sy}?", "options": YEAR_STATUS_OPTIONS,
                        "hours": h,
                        "why": f"ב{sy} למדת {payslips._fmt(h)} שעות, יותר מיחידה אחת. בשבתון ובחל\"ת התקרה של יחידה לשנה לא חלה."})

    missing.sort(key=lambda m: m["priority"])
    ready = bool(slip) and bool(t.get("reform_start_year")) and (bool(courses) or bool(t.get("u_pre")))
    nxt = missing[0] if missing else None
    if nxt and nxt["kind"] == "file":
        nxt = dict(nxt, guide=portal_guide(nxt["doc_type"]))
    return {
        "docs": docs, "known": known, "missing": missing, "ready_to_check": ready,
        "questions_left": max(0, 3 - case.get("questions_asked", 0)),
        "next": nxt,
    }


def progress(status):
    """0-100 לפס "התיק שלי"."""
    docs = status["docs"]
    read = sum(1 for d in docs if d["status"] == "read")
    base = int(70 * read / max(1, len(docs)))
    return min(100, base + (0 if status["missing"] else 20))
