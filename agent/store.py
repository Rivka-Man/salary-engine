"""
התיק של המורה ויומן הסוכן (טבלאות agent_cases, agent_events ב-schema.sql).

גם גרסה פנימית של שתי הפונקציות שיהודית מספקת (save_upload, portal_guide), כדי
שהסוכן יעבוד לפני שהן קיימות. כשהקבצים שלה קיימים (agent/uploads.py,
agent/portal_guide.json) משתמשים בהם.
"""
import json
import os
from datetime import datetime

import psycopg2

from db import get_connection

DOC_LABELS = {
    "payslip": "תלוש שכר",
    "courses": "הקורסים שלי",
    "oz_join": "הצטרפות לעוז לתמורה",
    "maktav": "מכתב נתוני העסקה",
    "unknown": "קובץ לא מזוהה",
}

# הקבצים שהסוכן מבקש, לפי הסדר. מכתב העסקה רק כשאי אפשר לשאול על כפל תואר (ראו case.py).
CORE_DOCS = ["payslip", "courses", "oz_join"]

# מדריך הפורטל (מהאפיון ומ"איך מביאים?" הקיים). יהודית מוציאה אותו ל-agent/portal_guide.json.
_BUILTIN_GUIDE = {
    "payslip": {"label": "תלוש שכר", "portal_path": "[רבקה משלימה]", "image": None,
                "why": "ממנו רואים כמה יחידות גמול משולמות לך היום (שורה 249)."},
    "courses": {"label": "הקורסים שלי", "portal_path": "פיתוח מקצועי והדרכה ← הקורסים שלי ← הדפס ← PDF",
                "image": "courses.png", "why": "ממנו מחשבים כמה יחידות מגיעות לך מההשתלמויות."},
    "oz_join": {"label": "הצטרפות לעוז לתמורה",
                "portal_path": "מרחב מנהלי ← אופק חדש ועוז לתמורה ← עוז לתמורה ← הצטרפות ויציאה ← הדפס ← PDF",
                "image": "oz_join.png", "why": "ממנו יודעים מאיזו שנה נספרות ההשתלמויות שלך."},
    "maktav": {"label": "מכתב נתוני העסקה",
               "portal_path": "שירות ותמיכה ← אישורים ← אישור נתוני העסקה ← הפקת אישור",
               "image": "maktav.png", "why": "ממנו יודעים אם מוכר לך גמול כפל תואר או ביניים."},
}


def _now():
    return datetime.now().isoformat(timespec="seconds")


def portal_guide(doc_type):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portal_guide.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if doc_type in data:
                return dict(_BUILTIN_GUIDE.get(doc_type, {}), **data[doc_type])
        except (OSError, ValueError):
            pass
    return dict(_BUILTIN_GUIDE.get(doc_type) or {"label": DOC_LABELS.get(doc_type, doc_type)})


def save_upload(teacher_id, filename, data, conn=None):
    """שומר קובץ שהמורה גררה, בלי סוג (הסוכן מזהה). מחזיר doc_id."""
    own = conn is None
    conn = conn or get_connection()
    row = conn.execute(
        "INSERT INTO teacher_documents (teacher_id, doc_type, filename, content, uploaded_at) "
        "VALUES (%s, 'unknown', %s, %s, %s) RETURNING doc_id",
        (teacher_id, filename, psycopg2.Binary(data), _now()),
    ).fetchone()
    conn.commit()
    if own:
        conn.close()
    return row["doc_id"]


def get_document(conn, teacher_id, doc_id):
    row = conn.execute(
        "SELECT doc_id, doc_type, filename, content FROM teacher_documents WHERE doc_id=%s AND teacher_id=%s",
        (doc_id, teacher_id),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["content"] = bytes(d["content"])
    return d


def set_doc_type(conn, doc_id, doc_type):
    conn.execute("UPDATE teacher_documents SET doc_type=%s WHERE doc_id=%s", (doc_type, doc_id))


# ------------------------------------------------------------------
# התיק
# ------------------------------------------------------------------

def empty_case():
    return {"docs": {}, "facts": [], "result": None, "sent": [], "questions_asked": 0,
            "pending": None, "asked_fields": []}


def load_case(conn, teacher_id):
    row = conn.execute("SELECT case_json, messages_json FROM agent_cases WHERE teacher_id=%s", (teacher_id,)).fetchone()
    if not row:
        return empty_case(), []
    case = dict(empty_case(), **json.loads(row["case_json"]))
    return case, json.loads(row["messages_json"])


def save_case(conn, teacher_id, case, messages):
    conn.execute(
        """INSERT INTO agent_cases (teacher_id, case_json, messages_json, updated_at) VALUES (%s, %s, %s, %s)
           ON CONFLICT (teacher_id) DO UPDATE SET case_json=EXCLUDED.case_json,
             messages_json=EXCLUDED.messages_json, updated_at=EXCLUDED.updated_at""",
        (teacher_id, json.dumps(case, ensure_ascii=False), json.dumps(messages, ensure_ascii=False), _now()),
    )
    conn.commit()


def reset_case(conn, teacher_id):
    """לדמו: התחלה מחדש של השיחה. לא נוגע בנתוני המורה."""
    conn.execute("DELETE FROM agent_cases WHERE teacher_id=%s", (teacher_id,))
    conn.commit()


def set_fact(case, key, label, value, source):
    case["facts"] = [f for f in case["facts"] if f["key"] != key]
    case["facts"].append({"key": key, "label": label, "value": value, "source": source})


def log_event(conn, teacher_id, kind, name=None, input_=None, output=None):
    try:
        conn.execute(
            "INSERT INTO agent_events (teacher_id, ts, kind, name, input_json, output_json) VALUES (%s,%s,%s,%s,%s,%s)",
            (teacher_id, _now(), kind, name,
             json.dumps(input_, ensure_ascii=False, default=str) if input_ is not None else None,
             json.dumps(output, ensure_ascii=False, default=str) if output is not None else None),
        )
        conn.commit()
    except Exception:  # יומן לא מפיל את השיחה
        conn.rollback()
