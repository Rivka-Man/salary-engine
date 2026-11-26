"""
סוכן הליווי של OptiPay. החוזה עם המסך: agent/contracts.py.

    from agent import run_agent, save_upload
"""
from agent.contracts import demo_run_agent  # noqa: F401
from agent.loop import run_agent  # noqa: F401


def save_upload(teacher_id, file_storage):
    """קובץ שהמורה גררה (werkzeug FileStorage) ← teacher_documents, בלי סוג. מחזיר doc_id."""
    from agent.store import save_upload as _save
    return _save(teacher_id, file_storage.filename, file_storage.read())


def reset_case(teacher_id):
    """לדמו: מתחילים את השיחה מחדש. נתוני המורה לא נמחקים."""
    from agent.store import reset_case as _reset
    from db import get_connection
    conn = get_connection()
    try:
        _reset(conn, teacher_id)
    finally:
        conn.close()
