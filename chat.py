"""
מסך הצ'אט של הסוכן למורה: /my/<token>/chat

המסך שולח אירועים (קבצים, תשובה, הודעה, "שלחתי") ל-run_agent ומצייר את
התשובה לפי החוזה ב-agent/contracts.py. בלי ANTHROPIC_API_KEY הסוכן רץ במסלול
הירוק (רשת הביטחון, בלי AI), וזה המסלול של הדמו.

בדמו לא גוררים קבצים: כפתורי "העלאה" שולחים קבצי דמו (agent/demo_docs.py),
שנקראים בדרך הרגילה. קבצים אמיתיים עדיין אפשר לצרף ב-+ או בגרירה.
"""
from flask import Blueprint, abort, jsonify, render_template, request

from agent import run_agent
from agent.demo_docs import make, reset_demo
from agent.store import DOC_LABELS, portal_guide, reset_case, save_upload
from db import get_connection

bp = Blueprint("chat", __name__)

DEMO_TEACHERS = {"T900"}  # קבצי דמו ואיפוס רק לשרה כהן, כדי לא לדרוס נתונים של מורה אמיתית
GUIDE_DOCS = ["payslip", "courses", "oz_join", "vacations", "maktav", "gmulim"]


def _teacher(token):
    conn = get_connection()
    try:
        t = conn.execute("SELECT teacher_id, full_name FROM teachers WHERE access_token=%s", (token,)).fetchone()
    finally:
        conn.close()
    if not t:
        abort(404)
    return t


def _first_name(full):
    return (full or "").split()[0] if full else ""


@bp.route("/my/<token>/chat")
def chat_page(token):
    t = _teacher(token)
    guides = {d: dict(portal_guide(d), label=portal_guide(d).get("label") or DOC_LABELS.get(d, d)) for d in GUIDE_DOCS}
    return render_template("chat.html", token=token, first_name=_first_name(t["full_name"]),
                           demo=t["teacher_id"] in DEMO_TEACHERS, guides=guides)


@bp.route("/my/<token>/chat/event", methods=["POST"])
def chat_event(token):
    t = _teacher(token)
    event = request.get_json(silent=True) or {}
    if event.get("type") not in ("resume", "files", "answer", "message", "sent"):
        abort(400)
    return jsonify(run_agent(t["teacher_id"], event))


@bp.route("/my/<token>/chat/upload", methods=["POST"])
def chat_upload(token):
    t = _teacher(token)
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        abort(400)
    ids = [save_upload(t["teacher_id"], f.filename, f.read()) for f in files]
    return jsonify(run_agent(t["teacher_id"], {"type": "files", "doc_ids": ids}))


@bp.route("/my/<token>/chat/demo-file", methods=["POST"])
def chat_demo_file(token):
    t = _teacher(token)
    if t["teacher_id"] not in DEMO_TEACHERS:
        abort(403)
    kinds = (request.get_json(silent=True) or {}).get("kinds") or []
    try:
        ids = [save_upload(t["teacher_id"], *make(k)) for k in kinds]
    except ValueError:
        abort(400)
    if not ids:
        abort(400)
    return jsonify(run_agent(t["teacher_id"], {"type": "files", "doc_ids": ids}))


@bp.route("/my/<token>/chat/new-check", methods=["POST"])
def chat_new_check(token):
    """תלוש חדש: שיחה חדשה. הקבצים, הקורסים והתשובות של המורה נשארים, רק התלוש מתחלף."""
    t = _teacher(token)
    conn = get_connection()
    try:
        reset_case(conn, t["teacher_id"])
    finally:
        conn.close()
    return jsonify({"ok": True})


@bp.route("/my/<token>/chat/reset", methods=["POST"])
def chat_reset(token):
    t = _teacher(token)
    if t["teacher_id"] not in DEMO_TEACHERS:
        abort(403)
    conn = get_connection()
    try:
        reset_demo(conn, t["teacher_id"])
    finally:
        conn.close()
    return jsonify({"ok": True})
