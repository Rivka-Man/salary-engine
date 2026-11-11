"""
טעינת הסכם חדש, הצגת השינויים שהוא מחייב בחוקות, אישור והטמעה, וגרסאות חוקה
עם שחזור.

הזרימה:
1. העלאת קובץ ההסכם (/constitutions/agreements). הקובץ נשמר ב-DB, ו-Claude
   מנתח אותו ברקע (agreement_ai.py) - הניתוח לוקח עד כמה דקות, יותר מזמן
   בקשה רגילה בשרת.
2. דף ההסכם מציג אילו חוקות מושפעות ואילו חוקים משתנים, עם לפני ← אחרי לכל
   שינוי. התצוגה מחושבת ע"י הרצה של השינויים בתוך טרנזקציה שמבוטלת בסוף
   (dry run) - כך מה שמוצג הוא בדיוק מה שיקרה באישור, כולל סגירת תקופות
   וחסימת תקופות חופפות.
3. אישור: לפני ההחלה נשמרת גרסה (תמונת מצב מלאה) של כל חוקה מושפעת, ואז
   השינויים שסומנו מוחלים יחד - כולם או אף אחד - ונרשמים ביומן השינויים.
4. גרסאות: דף לכל חוקה עם כל הגרסאות השמורות. שחזור מציג קודם מה ישתנה
   (אותו dry run), ושומר גרסה של המצב הנוכחי לפני שהוא מחליף אותו.
"""
import io
import json
import re
import threading
import traceback
from datetime import datetime, timedelta

import psycopg2
from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for

from agreement_ai import CHANGE_KINDS, MAX_FILE_BYTES, AgreementError, analyze_agreement
from backoffice import (PARAM_FIELDS, RULE_EDITABLE_FIELDS, RULE_ID_RE, _conditions_by_rule, _get_constitution,
                        _iso_date_or_none, _log_entries, _now, _row_snapshot, add_param_period_db, log_change,
                        save_rule_logic, validate_condition)
from db import get_connection
from rule_vocab import ACTION_LABELS as RULE_ACTION_LABELS

bp = Blueprint("agreements", __name__, url_prefix="/constitutions")


@bp.app_template_filter("fromjson")
def _fromjson(value):
    return json.loads(value) if value else {}

STATUS_LABELS = {
    "analyzing": ("בניתוח", "accent"),
    "ready": ("ממתין לאישור", "warn"),
    "failed": ("הניתוח נכשל", "critical"),
    "applied": ("הוטמע", "good"),
    "rejected": ("נדחה", "neutral"),
}
# ניתוח שלא הסתיים אחרי הזמן הזה כנראה נקטע (למשל השרת הופעל מחדש)
STALE_ANALYSIS = timedelta(minutes=15)

RULE_COLUMNS = ["rule_id", "topic", "title", "description", "source_doc", "source_ref", "priority", "applies_when",
                "valid_from", "valid_to", "overrides", "active", "constitution_id", "action", "result_value",
                "logic_seeded"]
COND_COLUMNS = ["connector", "field", "operator", "value", "value_field"]
RULE_EDITABLE = [f for f, _ in RULE_EDITABLE_FIELDS]


class NoChange(Exception):
    """השינוי כבר קיים בחוקה - אין מה להחיל."""


# ------------------------------------------------------------------
# גרסאות: תמונת מצב של חוקה ושחזור שלה
# ------------------------------------------------------------------

def snapshot_constitution(conn, constitution_id):
    c = _get_constitution(conn, constitution_id)
    rules = conn.execute(
        "SELECT * FROM rules WHERE constitution_id=%s ORDER BY priority, rule_id", (constitution_id,)
    ).fetchall()
    ids = [r["rule_id"] for r in rules]
    conds = _conditions_by_rule(conn, ids)
    params = conn.execute(
        "SELECT * FROM rule_parameters WHERE rule_id = ANY(%s) ORDER BY rule_id, param_name, valid_from NULLS FIRST",
        (ids,),
    ).fetchall() if ids else []
    return {
        "name": c["name"], "description": c["description"],
        "rules": [dict({k: r[k] for k in RULE_COLUMNS},
                       conditions=[{k: x[k] for k in COND_COLUMNS} for x in conds.get(r["rule_id"], [])],
                       parameters=[_row_snapshot(p, PARAM_FIELDS) for p in params if p["rule_id"] == r["rule_id"]])
                  for r in rules],
    }


def save_version(conn, constitution_id, label, agreement_id=None):
    row = conn.execute(
        "INSERT INTO constitution_versions (constitution_id, created_at, label, agreement_id, snapshot_json) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING version_id",
        (constitution_id, _now(), label, agreement_id,
         json.dumps(snapshot_constitution(conn, constitution_id), ensure_ascii=False)),
    ).fetchone()
    return row["version_id"]


def _code_rule_ids():
    from engine import RULES_METADATA  # בתוך הפונקציה: engine כבד, ונדרש רק בשחזור
    return {r["rule_id"] for r in RULES_METADATA}


def _param_key(p):
    return (p["param_name"], float(p["param_value"]), p["valid_from"], p["valid_to"])


def restore_snapshot(conn, constitution_id, snap, reason):
    """
    מחזיר את החוקה למצב שב-snap ורושם כל שינוי ביומן (source='restore'). לא
    עושה commit. מחזיר רשימת הערות (דברים שלא שוחזרו ולמה).
    """
    notes = []
    src = "restore"
    c = _get_constitution(conn, constitution_id)
    before = {"name": c["name"], "description": c["description"]}
    after = {"name": snap["name"], "description": snap["description"]}
    if before != after:
        conn.execute("UPDATE constitutions SET name=%s, description=%s WHERE constitution_id=%s",
                     (after["name"], after["description"], constitution_id))
        log_change(conn, constitution_id, "constitution", "update", before, after, reason, source=src)

    current = {r["rule_id"]: r for r in conn.execute(
        "SELECT * FROM rules WHERE constitution_id=%s", (constitution_id,)).fetchall()}
    snap_ids = set()
    for sr in snap["rules"]:
        rule_id = sr["rule_id"]
        snap_ids.add(rule_id)
        conds = [dict(x) for x in sr["conditions"]]
        rule = current.get(rule_id)
        if rule is None:
            if conn.execute("SELECT 1 FROM rules WHERE rule_id=%s", (rule_id,)).fetchone():
                notes.append(f"החוק {rule_id} שייך עכשיו לחוקה אחרת - לא שוחזר.")
                continue
            cols = [k for k in RULE_COLUMNS if k in sr]
            conn.execute(f"INSERT INTO rules ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                         [constitution_id if k == "constitution_id" else sr[k] for k in cols])
            for seq, x in enumerate(conds, start=1):
                conn.execute(
                    "INSERT INTO rule_conditions (rule_id, seq, connector, field, operator, value, value_field) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)", [rule_id, seq] + [x[k] for k in COND_COLUMNS])
            for p in sr["parameters"]:
                conn.execute("INSERT INTO rule_parameters (rule_id, param_name, param_value, valid_from, valid_to) "
                             "VALUES (%s, %s, %s, %s, %s)", [rule_id] + [p[k] for k in PARAM_FIELDS])
            log_change(conn, constitution_id, "rule", "insert", None,
                       {"rule_id": rule_id, "title": sr["title"], "priority": sr["priority"]},
                       reason, rule_id=rule_id, source=src)
            continue

        fields = RULE_EDITABLE + ["priority"]
        b, a = _row_snapshot(rule, fields), {k: sr.get(k) for k in fields}
        if b != a:
            conn.execute(f"UPDATE rules SET {', '.join(f'{k}=%s' for k in fields)} WHERE rule_id=%s",
                         [a[k] for k in fields] + [rule_id])
            log_change(conn, constitution_id, "rule", "update", b, a, reason, rule_id=rule_id, source=src)
        save_rule_logic(conn, constitution_id, rule, conds, sr.get("action"), sr.get("result_value"), reason,
                        source=src)

        cur_params = conn.execute("SELECT * FROM rule_parameters WHERE rule_id=%s", (rule_id,)).fetchall()
        want = {_param_key(p): p for p in sr["parameters"]}
        have = {_param_key(p): p for p in cur_params}
        for key, p in have.items():
            if key not in want:
                conn.execute("DELETE FROM rule_parameters WHERE param_id=%s", (p["param_id"],))
                log_change(conn, constitution_id, "parameter", "delete", _row_snapshot(p, PARAM_FIELDS), None,
                           reason, rule_id=rule_id, source=src)
        for key, p in want.items():
            if key not in have:
                conn.execute("INSERT INTO rule_parameters (rule_id, param_name, param_value, valid_from, valid_to) "
                             "VALUES (%s, %s, %s, %s, %s)", [rule_id] + [p[k] for k in PARAM_FIELDS])
                log_change(conn, constitution_id, "parameter", "insert", None, {k: p[k] for k in PARAM_FIELDS},
                           reason, rule_id=rule_id, source=src)

    code_rules = _code_rule_ids()
    for rule_id, rule in current.items():
        if rule_id in snap_ids:
            continue
        if rule_id in code_rules:
            notes.append(f"החוק {rule_id} לא היה בגרסה, אבל המנוע מחשב לפיו - לא נמחק.")
            continue
        conn.execute("DELETE FROM rule_parameters WHERE rule_id=%s", (rule_id,))
        conn.execute("DELETE FROM rule_conditions WHERE rule_id=%s", (rule_id,))
        conn.execute("DELETE FROM rules WHERE rule_id=%s", (rule_id,))
        log_change(conn, constitution_id, "rule", "delete",
                   {"rule_id": rule_id, "title": rule["title"], "priority": rule["priority"]}, None,
                   reason, rule_id=rule_id, source=src)
    return notes


# ------------------------------------------------------------------
# החלת שינוי מוצע אחד
# ------------------------------------------------------------------

def _apply_change(conn, ch, reason):
    """
    מחיל שינוי אחד מההצעה ורושם ביומן (source='agreement'). מחזיר
    constitution_id. מעלה ValueError (שגיאה) או NoChange.
    """
    kind = ch.get("kind")
    rule_id = (ch.get("rule_id") or "").strip()
    fields = ch.get("fields") or {}
    src = "agreement"

    if kind == "rule_new":
        cid = ch.get("constitution_id")
        if not cid or not _get_constitution(conn, cid):
            raise ValueError("לא צוינה חוקה קיימת לחוק החדש.")
        if not RULE_ID_RE.match(rule_id):
            raise ValueError(f"קוד חוק לא תקין: {rule_id}.")
        if conn.execute("SELECT 1 FROM rules WHERE rule_id=%s", (rule_id,)).fetchone():
            raise ValueError(f"כבר קיים חוק עם הקוד {rule_id}.")
        title = (fields.get("title") or "").strip()
        if not title:
            raise ValueError("לחוק החדש חסרה כותרת.")
        vf, vt = _iso_date_or_none(fields.get("valid_from")), _iso_date_or_none(fields.get("valid_to"))
        constitution = _get_constitution(conn, cid)
        conn.execute(
            "INSERT INTO rules (rule_id, topic, title, description, source_doc, source_ref, priority, applies_when, "
            "valid_from, valid_to, active, constitution_id, logic_seeded) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, 1)",
            (rule_id, constitution["name"], title, fields.get("description") or title,
             fields.get("source_doc") or "—", fields.get("source_ref"), int(ch.get("priority") or 0),
             fields.get("applies_when"), vf, vt, cid),
        )
        log_change(conn, cid, "rule", "insert", None,
                   {"rule_id": rule_id, "title": title, "priority": int(ch.get("priority") or 0)},
                   reason, rule_id=rule_id, source=src)
        rule = conn.execute("SELECT * FROM rules WHERE rule_id=%s", (rule_id,)).fetchone()
        _apply_logic(conn, cid, rule, ch, reason, allow_same=True)
        return cid

    rule = conn.execute("SELECT * FROM rules WHERE rule_id=%s", (rule_id,)).fetchone()
    if not rule:
        raise ValueError(f"החוק {rule_id} לא קיים במערכת.")
    cid = rule["constitution_id"]

    if kind == "param_period":
        try:
            value = float(ch.get("param_value"))
        except (TypeError, ValueError):
            raise ValueError("חסר ערך מספרי לפרמטר.")
        vf, vt = _iso_date_or_none(ch.get("valid_from")), _iso_date_or_none(ch.get("valid_to"))
        name = ch.get("param_name") or ""
        open_now = conn.execute(
            "SELECT 1 FROM rule_parameters WHERE rule_id=%s AND param_name=%s AND param_value=%s "
            "AND valid_from IS NOT DISTINCT FROM %s AND valid_to IS NOT DISTINCT FROM %s",
            (rule_id, name, value, vf, vt)).fetchone()
        if open_now:
            raise NoChange()
        try:
            add_param_period_db(conn, cid, rule_id, name, value, vf, vt, reason, source=src)
        except ValueError as exc:
            if "כבר יש את הערך" in str(exc):
                raise NoChange()
            raise
        return cid

    if kind == "rule_update":
        changes = {k: (v.strip() if isinstance(v, str) else v) for k, v in fields.items()
                   if k in RULE_EDITABLE and v is not None}
        for k in ("valid_from", "valid_to"):
            if k in changes:
                changes[k] = _iso_date_or_none(changes[k])
        if not changes:
            raise ValueError("לא צוין אף שדה לעדכון.")
        before = _row_snapshot(rule, list(changes))
        if before == changes:
            raise NoChange()
        conn.execute(f"UPDATE rules SET {', '.join(f'{k}=%s' for k in changes)} WHERE rule_id=%s",
                     list(changes.values()) + [rule_id])
        log_change(conn, cid, "rule", "update", before, changes, reason, rule_id=rule_id, source=src)
        return cid

    if kind == "rule_logic":
        _apply_logic(conn, cid, rule, ch, reason)
        return cid

    raise ValueError(f"סוג שינוי לא מוכר: {kind}.")


def _apply_logic(conn, cid, rule, ch, reason, allow_same=False):
    conds = [validate_condition(c.get("field"), c.get("operator"), c.get("connector"), c.get("value"),
                                c.get("value_field"))
             for c in (ch.get("conditions") or [])]
    action = ch.get("action") or None
    if action and action not in RULE_ACTION_LABELS:
        raise ValueError(f"פעולה לא מוכרת: {action}.")
    result = (ch.get("result_value") or "").strip() or None
    if save_rule_logic(conn, cid, rule, conds, action, result, reason, source="agreement") is None and not allow_same:
        raise NoChange()


def _max_change_id(conn):
    return conn.execute("SELECT COALESCE(MAX(change_id), 0) AS m FROM constitution_change_log").fetchone()["m"]


def run_changes(conn, changes, indexes, reason):
    """
    מריץ את השינויים שבאינדקסים, כל אחד ב-savepoint משלו, ומחזיר לכל אחד
    תוצאה: status ('ok' / 'error' / 'no_change'), constitution_id, וה-log
    שנכתב בגללו. לא עושה commit ולא rollback - זה בידי הקורא.
    """
    results = {}
    indexes = list(indexes)
    # שלב 1: מה כבר קיים בחוקה כפי שהיא עכשיו (כל שינוי לבד). אחרת הסכם
    # שחוזר על ערך קיים היה מתנגש בתקופה החדשה שהוסיף שינוי אחר באותו הסכם.
    for i in indexes:
        conn.execute("SAVEPOINT probe_sp")
        try:
            _apply_change(conn, changes[i], reason)
        except NoChange:
            results[i] = dict(status="no_change", constitution_id=None, log=[])
        except Exception:  # noqa: BLE001 - שגיאות מדווחות בשלב 2
            pass
        conn.execute("ROLLBACK TO SAVEPOINT probe_sp")
    # שלב 2: החלה של השאר ברצף, כמו באישור
    for i in indexes:
        if i in results:
            continue
        ch = changes[i]
        conn.execute("SAVEPOINT change_sp")
        start = _max_change_id(conn)
        try:
            cid = _apply_change(conn, ch, reason)
            log = conn.execute("SELECT * FROM constitution_change_log WHERE change_id > %s ORDER BY change_id",
                               (start,)).fetchall()
            conn.execute("RELEASE SAVEPOINT change_sp")
            results[i] = dict(status="ok", constitution_id=cid, log=_log_entries(log))
        except NoChange:
            conn.execute("ROLLBACK TO SAVEPOINT change_sp")
            results[i] = dict(status="no_change", constitution_id=None, log=[])
        except Exception as exc:  # noqa: BLE001 - ההצעה מגיעה מ-AI; כל קלט שבור הוא שגיאה בשורה שלו
            conn.execute("ROLLBACK TO SAVEPOINT change_sp")
            results[i] = dict(status="error", error=str(exc).strip(), constitution_id=None, log=[])
    return results


def _target_constitution(conn, ch):
    if ch.get("kind") == "rule_new":
        return ch.get("constitution_id")
    row = conn.execute("SELECT constitution_id FROM rules WHERE rule_id=%s", (ch.get("rule_id"),)).fetchone()
    return row["constitution_id"] if row else None


# ------------------------------------------------------------------
# ניתוח ברקע
# ------------------------------------------------------------------

def _run_analysis(agreement_id):
    conn = get_connection()
    try:
        row = conn.execute("SELECT filename, content FROM agreements WHERE agreement_id=%s",
                           (agreement_id,)).fetchone()
        result = analyze_agreement(conn, row["filename"], bytes(row["content"]))
        conn.rollback()
        conn.execute(
            "UPDATE agreements SET status='ready', title=%s, summary=%s, proposal_json=%s, error=NULL "
            "WHERE agreement_id=%s",
            ((result.get("agreement_title") or "").strip() or row["filename"], result.get("summary"),
             json.dumps(result, ensure_ascii=False), agreement_id),
        )
    except Exception as exc:  # noqa: BLE001 - כל כשל נשמר כהודעה בדף ההסכם
        if not isinstance(exc, AgreementError):
            traceback.print_exc()
        conn.rollback()
        msg = str(exc) if isinstance(exc, AgreementError) else f"שגיאה לא צפויה בניתוח: {exc}"
        conn.execute("UPDATE agreements SET status='failed', error=%s WHERE agreement_id=%s", (msg, agreement_id))
    conn.commit()
    conn.close()


def start_analysis(agreement_id):
    threading.Thread(target=_run_analysis, args=(agreement_id,), daemon=True).start()


# ------------------------------------------------------------------
# נתיבים: הסכמים
# ------------------------------------------------------------------

def _get_agreement(conn, agreement_id):
    row = conn.execute(
        "SELECT agreement_id, title, filename, uploaded_at, status, summary, proposal_json, applied_json, error, "
        "applied_at FROM agreements WHERE agreement_id=%s", (agreement_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    return row


@bp.route("/agreements")
def agreements():
    conn = get_connection()
    rows = conn.execute(
        "SELECT agreement_id, title, filename, uploaded_at, status, proposal_json, applied_at "
        "FROM agreements ORDER BY agreement_id DESC").fetchall()
    conn.close()
    items = []
    for r in rows:
        proposal = json.loads(r["proposal_json"]) if r["proposal_json"] else None
        items.append(dict(r, change_count=len(proposal["changes"]) if proposal else None,
                          status_label=STATUS_LABELS.get(r["status"], (r["status"], "neutral"))))
    return render_template("agreements.html", agreements=items)


@bp.route("/agreements/upload", methods=["POST"])
def upload_agreement():
    f = request.files.get("agreement_file")
    if not f or not f.filename:
        flash("יש לבחור קובץ הסכם.", "error")
        return redirect(url_for("agreements.agreements"))
    data = f.read()
    if not re.search(r"\.(pdf|docx|txt)$", f.filename, re.I):
        flash("אפשר לטעון PDF, Word (docx) או טקסט (txt).", "error")
        return redirect(url_for("agreements.agreements"))
    if len(data) > MAX_FILE_BYTES:
        flash("הקובץ גדול מדי (עד 25MB).", "error")
        return redirect(url_for("agreements.agreements"))
    conn = get_connection()
    row = conn.execute(
        "INSERT INTO agreements (title, filename, content, uploaded_at, status) "
        "VALUES (%s, %s, %s, %s, 'analyzing') RETURNING agreement_id",
        ((request.form.get("title") or "").strip() or None, f.filename, psycopg2.Binary(data), _now()),
    ).fetchone()
    conn.commit()
    conn.close()
    start_analysis(row["agreement_id"])
    flash("ההסכם נטען ונשלח לניתוח. זה לוקח בדרך כלל דקה עד כמה דקות.", "success")
    return redirect(url_for("agreements.agreement_detail", agreement_id=row["agreement_id"]))


def _preview(conn, proposal):
    """מריץ את כל השינויים ב-dry run ומחזיר שורות לתצוגה + החוקות המושפעות."""
    changes = proposal.get("changes") or []
    results = run_changes(conn, changes, range(len(changes)), "תצוגה מקדימה")
    conn.rollback()
    names = {c["constitution_id"]: c["name"] for c in
             conn.execute("SELECT constitution_id, name FROM constitutions").fetchall()}
    titles = {r["rule_id"]: r["title"] for r in conn.execute("SELECT rule_id, title FROM rules").fetchall()}
    rows = []
    for i, ch in enumerate(changes):
        res = results[i]
        cid = res["constitution_id"] or _target_constitution(conn, ch)
        rows.append(dict(
            res, idx=i, ch=ch, kind_label=CHANGE_KINDS.get(ch.get("kind"), ch.get("kind")),
            rule_title=titles.get(ch.get("rule_id")) or (ch.get("fields") or {}).get("title"),
            constitution_id=cid, constitution_name=names.get(cid, "לא משויך לחוקה"),
        ))
    groups = {}
    for r in rows:
        groups.setdefault((r["constitution_name"], r["constitution_id"]), []).append(r)
    affected = [dict(name=name, constitution_id=cid,
                     ok=sum(1 for r in g if r["status"] == "ok"),
                     rules=sorted({r["ch"].get("rule_id") for r in g if r["status"] == "ok"}))
                for (name, cid), g in groups.items()]
    return groups, [x for x in affected if x["ok"]]


@bp.route("/agreements/<int:agreement_id>")
def agreement_detail(agreement_id):
    conn = get_connection()
    a = _get_agreement(conn, agreement_id)
    proposal = json.loads(a["proposal_json"]) if a["proposal_json"] else None
    applied = json.loads(a["applied_json"]) if a["applied_json"] else None
    groups, affected = ({}, [])
    if proposal and a["status"] == "ready":
        groups, affected = _preview(conn, proposal)
    versions = []
    if applied:
        versions = conn.execute(
            "SELECT v.version_id, v.constitution_id, v.created_at, c.name FROM constitution_versions v "
            "JOIN constitutions c ON c.constitution_id=v.constitution_id WHERE v.agreement_id=%s",
            (agreement_id,)).fetchall()
    conn.close()
    stale = (a["status"] == "analyzing"
             and datetime.fromisoformat(a["uploaded_at"]) < datetime.now() - STALE_ANALYSIS)
    return render_template(
        "agreement_detail.html", a=a, proposal=proposal, applied=applied, groups=groups, affected=affected,
        versions=versions, stale=stale, status_label=STATUS_LABELS.get(a["status"], (a["status"], "neutral")),
    )


@bp.route("/agreements/<int:agreement_id>/file")
def agreement_file(agreement_id):
    conn = get_connection()
    row = conn.execute("SELECT filename, content FROM agreements WHERE agreement_id=%s", (agreement_id,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    return send_file(io.BytesIO(bytes(row["content"])), download_name=row["filename"], as_attachment=True)


@bp.route("/agreements/<int:agreement_id>/reanalyze", methods=["POST"])
def reanalyze_agreement(agreement_id):
    conn = get_connection()
    a = _get_agreement(conn, agreement_id)
    if a["status"] in ("applied", "rejected"):
        conn.close()
        flash("ההסכם כבר טופל.", "error")
        return redirect(url_for("agreements.agreement_detail", agreement_id=agreement_id))
    conn.execute("UPDATE agreements SET status='analyzing', error=NULL, uploaded_at=%s WHERE agreement_id=%s",
                 (_now(), agreement_id))
    conn.commit()
    conn.close()
    start_analysis(agreement_id)
    flash("ההסכם נשלח לניתוח מחדש.", "success")
    return redirect(url_for("agreements.agreement_detail", agreement_id=agreement_id))


@bp.route("/agreements/<int:agreement_id>/reject", methods=["POST"])
def reject_agreement(agreement_id):
    conn = get_connection()
    a = _get_agreement(conn, agreement_id)
    if a["status"] == "applied":
        conn.close()
        flash("ההסכם כבר הוטמע. כדי לבטל אותו משחזרים את הגרסה שלפניו.", "error")
        return redirect(url_for("agreements.agreement_detail", agreement_id=agreement_id))
    conn.execute("UPDATE agreements SET status='rejected' WHERE agreement_id=%s", (agreement_id,))
    conn.commit()
    conn.close()
    flash("ההסכם סומן כנדחה. החוקות לא שונו.", "success")
    return redirect(url_for("agreements.agreements"))


@bp.route("/agreements/<int:agreement_id>/apply", methods=["POST"])
def apply_agreement(agreement_id):
    back = redirect(url_for("agreements.agreement_detail", agreement_id=agreement_id))
    conn = get_connection()
    a = _get_agreement(conn, agreement_id)
    if a["status"] != "ready":
        conn.close()
        flash("ההסכם לא ממתין לאישור.", "error")
        return back
    changes = json.loads(a["proposal_json"]).get("changes") or []
    try:
        selected = sorted({int(x) for x in request.form.getlist("change")})
    except ValueError:
        selected = []
    selected = [i for i in selected if 0 <= i < len(changes)]
    if not selected:
        conn.close()
        flash("לא סומן אף שינוי.", "error")
        return back

    title = a["title"] or a["filename"]
    note = (request.form.get("reason") or "").strip()
    reason = f"הסכם: {title}" + (f" — {note}" if note else "")

    # גרסה לפני ההחלה, לכל חוקה שהשינויים נוגעים בה
    versions = {}
    for i in selected:
        cid = _target_constitution(conn, changes[i])
        if cid and cid not in versions and _get_constitution(conn, cid):
            versions[cid] = save_version(conn, cid, f"לפני הטמעת הסכם: {title}", agreement_id)

    results = run_changes(conn, changes, selected, reason)
    errors = [(i, r["error"]) for i, r in results.items() if r["status"] == "error"]
    if errors:
        conn.rollback()
        conn.close()
        for i, err in errors:
            flash(f"שינוי {i + 1} ({changes[i].get('rule_id')}): {err}", "error")
        flash("לא הוטמע אף שינוי. אפשר לבטל את סימון השינויים השגויים ולאשר שוב.", "error")
        return back
    applied_ok = [i for i, r in results.items() if r["status"] == "ok"]
    if not applied_ok:
        conn.rollback()
        conn.close()
        flash("כל השינויים שסומנו כבר קיימים בחוקה - לא היה מה להטמיע.", "error")
        return back

    applied = {"applied": applied_ok, "no_change": [i for i, r in results.items() if r["status"] == "no_change"],
               "versions": {str(k): v for k, v in versions.items()}, "note": note or None}
    conn.execute("UPDATE agreements SET status='applied', applied_at=%s, applied_json=%s WHERE agreement_id=%s",
                 (_now(), json.dumps(applied, ensure_ascii=False), agreement_id))
    conn.commit()
    conn.close()
    flash(f"הוטמעו {len(applied_ok)} שינויים בחוקה. הגרסה הקודמת נשמרה וניתן לשחזר אותה.", "success")
    return back


# ------------------------------------------------------------------
# נתיבים: גרסאות ושחזור
# ------------------------------------------------------------------

def _constitution_or_404(conn, constitution_id):
    c = _get_constitution(conn, constitution_id)
    if not c:
        conn.close()
        abort(404)
    return c


@bp.route("/<int:constitution_id>/versions")
def constitution_versions(constitution_id):
    conn = get_connection()
    constitution = _constitution_or_404(conn, constitution_id)
    versions = conn.execute(
        "SELECT v.version_id, v.created_at, v.label, v.agreement_id, a.title AS agreement_title "
        "FROM constitution_versions v LEFT JOIN agreements a ON a.agreement_id=v.agreement_id "
        "WHERE v.constitution_id=%s ORDER BY v.version_id DESC", (constitution_id,)).fetchall()
    applied = conn.execute(
        "SELECT agreement_id, title, filename, applied_at FROM agreements WHERE status='applied' "
        "AND agreement_id IN (SELECT agreement_id FROM constitution_versions WHERE constitution_id=%s) "
        "ORDER BY applied_at DESC", (constitution_id,)).fetchall()
    conn.close()
    return render_template("constitution_versions.html", constitution=constitution, versions=versions,
                           applied=applied)


def _version_or_404(conn, constitution_id, version_id):
    v = conn.execute("SELECT * FROM constitution_versions WHERE version_id=%s AND constitution_id=%s",
                     (version_id, constitution_id)).fetchone()
    if not v:
        conn.close()
        abort(404)
    return v


@bp.route("/<int:constitution_id>/versions/<int:version_id>/restore", methods=["GET", "POST"])
def restore_version(constitution_id, version_id):
    conn = get_connection()
    constitution = _constitution_or_404(conn, constitution_id)
    v = _version_or_404(conn, constitution_id, version_id)
    snap = json.loads(v["snapshot_json"])
    reason = f"שחזור לגרסה #{version_id} ({v['label']})"
    note = (request.form.get("reason") or "").strip()
    if note:
        reason += f" — {note}"

    if request.method == "POST":
        new_version = save_version(conn, constitution_id, f"לפני שחזור לגרסה #{version_id}")
        start = _max_change_id(conn)
        notes = restore_snapshot(conn, constitution_id, snap, reason)
        changed = _max_change_id(conn) > start
        if not changed:
            conn.rollback()
            conn.close()
            flash("החוקה כבר זהה לגרסה הזו - לא היה מה לשחזר.", "success")
            return redirect(url_for("agreements.constitution_versions", constitution_id=constitution_id))
        conn.commit()
        conn.close()
        for n in notes:
            flash(n, "error")
        flash(f"החוקה שוחזרה לגרסה #{version_id}. המצב שלפני השחזור נשמר כגרסה #{new_version}.", "success")
        return redirect(url_for("agreements.constitution_versions", constitution_id=constitution_id))

    # תצוגה מקדימה: שחזור בתוך טרנזקציה שמבוטלת
    start = _max_change_id(conn)
    notes = restore_snapshot(conn, constitution_id, snap, reason)
    log = conn.execute("SELECT * FROM constitution_change_log WHERE change_id > %s ORDER BY change_id DESC",
                       (start,)).fetchall()
    log = _log_entries(log)
    conn.rollback()
    conn.close()
    return render_template("constitution_restore.html", constitution=constitution, version=v, log=log,
                           notes=notes)
