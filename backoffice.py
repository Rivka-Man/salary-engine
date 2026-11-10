"""
בקאופיס החוקות: צפייה ועריכה של החוקות, החוקים והפרמטרים שלהם, עם יומן שינויים.

מה ניתן לעריכה כאן ומה לא:
- מטא-דאטה של חוק (כותרת, תיאור, מקור, סעיף, תנאי הפעלה, תוקף) - תיעוד בלבד,
  לא משפיע על החישוב.
- ערכי הפרמטרים (rule_parameters) ותקופות התוקף שלהם - משפיעים ישירות על
  החישוב: engine.get_param() שולף את הערך לפי rule_id + param_name + תאריך.
- הלוגיקה (איזה חוק חל על מי) נשארת קוד ב-engine.py, ולא נערכת מכאן.

ההגנה (סיסמת מנהל) מגיעה מ-require_admin_login ב-app.py, שחל על כל הנתיבים.
"""
import json
import re
from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for

from db import get_connection

bp = Blueprint("backoffice", __name__, url_prefix="/constitutions")

# שדות החוק שניתנים לעריכה מהבקאופיס. priority/overrides/active לא כאן - הם
# משקפים את סדר ומבנה הלוגיקה בקוד, ושינוי שלהם ב-DB לא היה משנה את החישוב.
RULE_EDITABLE_FIELDS = [
    ("title", "כותרת"),
    ("description", "תיאור"),
    ("source_doc", "מסמך מקור"),
    ("source_ref", "סעיף במקור"),
    ("applies_when", "תנאי הפעלה"),
    ("valid_from", "בתוקף מ-"),
    ("valid_to", "בתוקף עד"),
]
CONSTITUTION_EDITABLE_FIELDS = [("name", "שם"), ("description", "תיאור")]
PARAM_FIELD_LABELS = {
    "param_name": "פרמטר", "param_value": "ערך", "valid_from": "בתוקף מ-", "valid_to": "בתוקף עד",
}
FIELD_LABELS = dict(RULE_EDITABLE_FIELDS + CONSTITUTION_EDITABLE_FIELDS, **PARAM_FIELD_LABELS)

ENTITY_LABELS = {"constitution": "חוקה", "rule": "חוק", "parameter": "פרמטר"}
ACTION_LABELS = {"insert": "הוספה", "update": "עדכון", "delete": "מחיקה"}


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _iso_date_or_none(value):
    value = (value or "").strip()
    if not value:
        return None
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        raise ValueError(f"תאריך לא תקין: {value}")
    return value


def _row_snapshot(row, fields):
    return {f: row[f] for f in fields}


def log_change(conn, constitution_id, entity, action, before, after, reason, rule_id=None):
    conn.execute(
        "INSERT INTO constitution_change_log "
        "(constitution_id, rule_id, entity, action, before_json, after_json, reason, changed_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            constitution_id, rule_id, entity, action,
            json.dumps(before, ensure_ascii=False) if before is not None else None,
            json.dumps(after, ensure_ascii=False) if after is not None else None,
            (reason or "").strip() or None, _now(),
        ),
    )


def _get_constitution(conn, constitution_id):
    return conn.execute("SELECT * FROM constitutions WHERE constitution_id=%s", (constitution_id,)).fetchone()


def _get_rule(conn, constitution_id, rule_id):
    return conn.execute(
        "SELECT * FROM rules WHERE rule_id=%s AND constitution_id=%s", (rule_id, constitution_id)
    ).fetchone()


def _param_periods(conn, rule_id, param_name):
    return conn.execute(
        "SELECT * FROM rule_parameters WHERE rule_id=%s AND param_name=%s "
        "ORDER BY valid_from NULLS FIRST, param_id",
        (rule_id, param_name),
    ).fetchall()


def _check_periods(periods):
    """
    בודק שתקופות התוקף של פרמטר אחד לא חופפות - אחרת get_param() היה בוחר
    ערך לפי סדר מקרי. מחזיר הודעת שגיאה או None. NULL = פתוח לאותו כיוון.
    """
    for p in periods:
        if p["valid_from"] and p["valid_to"] and p["valid_from"] >= p["valid_to"]:
            return f"תאריך הסיום ({p['valid_to']}) חייב להיות אחרי תאריך ההתחלה ({p['valid_from']})."
    ordered = sorted(periods, key=lambda p: p["valid_from"] or "")
    for a, b in zip(ordered, ordered[1:]):
        if a["valid_to"] is None or b["valid_from"] is None or a["valid_to"] > b["valid_from"]:
            return (f"תקופות חופפות: {a['param_value']} ({a['valid_from'] or 'מאז ומתמיד'} - "
                    f"{a['valid_to'] or 'ללא סיום'}) ו-{b['param_value']} ({b['valid_from'] or 'מאז ומתמיד'} - "
                    f"{b['valid_to'] or 'ללא סיום'}).")
    return None


def _param_gaps(periods):
    """פערים בין תקופות (תאריכים שבהם אין ערך) - אזהרה בלבד, לא חוסם שמירה."""
    ordered = sorted(periods, key=lambda p: p["valid_from"] or "")
    return [(a["valid_to"], b["valid_from"]) for a, b in zip(ordered, ordered[1:])
            if a["valid_to"] and b["valid_from"] and a["valid_to"] < b["valid_from"]]


def _changed_fields(before, after):
    """רשימת (שדה, לפני, אחרי) לתצוגה ביומן."""
    before, after = before or {}, after or {}
    keys = list(dict.fromkeys(list(before) + list(after)))
    return [(FIELD_LABELS.get(k, k), before.get(k), after.get(k))
            for k in keys if before.get(k) != after.get(k)]


# ------------------------------------------------------------------
# רשימת חוקות
# ------------------------------------------------------------------

@bp.route("/")
def constitutions():
    conn = get_connection()
    rows = conn.execute("""
        SELECT c.*,
               (SELECT COUNT(*) FROM rules r WHERE r.constitution_id=c.constitution_id) AS rule_count,
               (SELECT COUNT(*) FROM rule_parameters p JOIN rules r ON r.rule_id=p.rule_id
                 WHERE r.constitution_id=c.constitution_id) AS param_count,
               (SELECT MAX(changed_at) FROM constitution_change_log l
                 WHERE l.constitution_id=c.constitution_id) AS last_change
        FROM constitutions c ORDER BY c.name
    """).fetchall()
    conn.close()
    return render_template("constitutions.html", constitutions=rows)


# ------------------------------------------------------------------
# חוקה: חוקים + פרמטרים
# ------------------------------------------------------------------

@bp.route("/<int:constitution_id>")
def constitution_detail(constitution_id):
    conn = get_connection()
    constitution = _get_constitution(conn, constitution_id)
    if not constitution:
        conn.close()
        flash("חוקה לא נמצאה.", "error")
        return redirect(url_for("backoffice.constitutions"))
    rules = conn.execute(
        "SELECT * FROM rules WHERE constitution_id=%s ORDER BY priority, rule_id", (constitution_id,)
    ).fetchall()
    params = conn.execute("""
        SELECT p.* FROM rule_parameters p JOIN rules r ON r.rule_id=p.rule_id
        WHERE r.constitution_id=%s ORDER BY p.param_name, p.valid_from NULLS FIRST
    """, (constitution_id,)).fetchall()
    conn.close()
    params_by_rule = {}
    for p in params:
        params_by_rule.setdefault(p["rule_id"], []).append(p)
    return render_template(
        "constitution_detail.html", constitution=constitution, rules=rules, params_by_rule=params_by_rule,
    )


@bp.route("/<int:constitution_id>/edit", methods=["POST"])
def edit_constitution(constitution_id):
    conn = get_connection()
    constitution = _get_constitution(conn, constitution_id)
    if not constitution:
        conn.close()
        flash("חוקה לא נמצאה.", "error")
        return redirect(url_for("backoffice.constitutions"))
    fields = [f for f, _ in CONSTITUTION_EDITABLE_FIELDS]
    after = {f: (request.form.get(f) or "").strip() or None for f in fields}
    if not after["name"]:
        conn.close()
        flash("יש למלא שם לחוקה.", "error")
        return redirect(url_for("backoffice.constitution_detail", constitution_id=constitution_id))
    before = _row_snapshot(constitution, fields)
    if before != after:
        conn.execute("UPDATE constitutions SET name=%s, description=%s WHERE constitution_id=%s",
                     (after["name"], after["description"], constitution_id))
        log_change(conn, constitution_id, "constitution", "update", before, after, request.form.get("reason"))
        conn.commit()
        flash("פרטי החוקה עודכנו.", "success")
    conn.close()
    return redirect(url_for("backoffice.constitution_detail", constitution_id=constitution_id))


# ------------------------------------------------------------------
# חוק בודד: עריכת מטא-דאטה + ניהול תקופות הפרמטרים
# ------------------------------------------------------------------

def _rule_or_redirect(conn, constitution_id, rule_id):
    rule = _get_rule(conn, constitution_id, rule_id)
    if not rule:
        conn.close()
        flash("חוק לא נמצא בחוקה הזו.", "error")
        return None, redirect(url_for("backoffice.constitution_detail", constitution_id=constitution_id))
    return rule, None


def _back_to_rule(constitution_id, rule_id):
    return redirect(url_for("backoffice.rule_detail", constitution_id=constitution_id, rule_id=rule_id))


@bp.route("/<int:constitution_id>/rules/<rule_id>", methods=["GET", "POST"])
def rule_detail(constitution_id, rule_id):
    conn = get_connection()
    rule, resp = _rule_or_redirect(conn, constitution_id, rule_id)
    if resp:
        return resp

    if request.method == "POST":
        fields = [f for f, _ in RULE_EDITABLE_FIELDS]
        try:
            after = {f: (request.form.get(f) or "").strip() or None for f in fields}
            after["valid_from"] = _iso_date_or_none(after["valid_from"])
            after["valid_to"] = _iso_date_or_none(after["valid_to"])
        except ValueError as exc:
            conn.close()
            flash(str(exc), "error")
            return _back_to_rule(constitution_id, rule_id)
        if not after["title"] or not after["description"] or not after["source_doc"]:
            conn.close()
            flash("כותרת, תיאור ומסמך מקור הם שדות חובה.", "error")
            return _back_to_rule(constitution_id, rule_id)
        before = _row_snapshot(rule, fields)
        if before != after:
            conn.execute(
                "UPDATE rules SET title=%s, description=%s, source_doc=%s, source_ref=%s, "
                "applies_when=%s, valid_from=%s, valid_to=%s WHERE rule_id=%s",
                (after["title"], after["description"], after["source_doc"], after["source_ref"],
                 after["applies_when"], after["valid_from"], after["valid_to"], rule_id),
            )
            log_change(conn, constitution_id, "rule", "update", before, after,
                       request.form.get("reason"), rule_id=rule_id)
            conn.commit()
            flash("פרטי החוק עודכנו.", "success")
        else:
            flash("לא בוצעו שינויים.", "success")
        conn.close()
        return _back_to_rule(constitution_id, rule_id)

    constitution = _get_constitution(conn, constitution_id)
    params = conn.execute(
        "SELECT * FROM rule_parameters WHERE rule_id=%s ORDER BY param_name, valid_from NULLS FIRST, param_id",
        (rule_id,),
    ).fetchall()
    log = conn.execute(
        "SELECT * FROM constitution_change_log WHERE rule_id=%s ORDER BY change_id DESC LIMIT 20", (rule_id,)
    ).fetchall()
    conn.close()
    param_groups = {}
    for p in params:
        param_groups.setdefault(p["param_name"], []).append(p)
    gaps = {name: _param_gaps(rows) for name, rows in param_groups.items()}
    return render_template(
        "rule_detail.html", constitution=constitution, rule=rule, param_groups=param_groups, gaps=gaps,
        log=_log_entries(log),
    )


PARAM_FIELDS = ["param_name", "param_value", "valid_from", "valid_to"]


def _parse_param_form(form):
    value = (form.get("param_value") or "").strip()
    try:
        value = float(value)
    except ValueError:
        raise ValueError("ערך הפרמטר חייב להיות מספר.")
    return value, _iso_date_or_none(form.get("valid_from")), _iso_date_or_none(form.get("valid_to"))


@bp.route("/<int:constitution_id>/rules/<rule_id>/params/<int:param_id>", methods=["POST"])
def edit_param(constitution_id, rule_id, param_id):
    conn = get_connection()
    rule, resp = _rule_or_redirect(conn, constitution_id, rule_id)
    if resp:
        return resp
    param = conn.execute(
        "SELECT * FROM rule_parameters WHERE param_id=%s AND rule_id=%s", (param_id, rule_id)
    ).fetchone()
    if not param:
        conn.close()
        flash("פרמטר לא נמצא.", "error")
        return _back_to_rule(constitution_id, rule_id)
    try:
        value, valid_from, valid_to = _parse_param_form(request.form)
    except ValueError as exc:
        conn.close()
        flash(str(exc), "error")
        return _back_to_rule(constitution_id, rule_id)

    before = _row_snapshot(param, PARAM_FIELDS)
    after = dict(before, param_value=value, valid_from=valid_from, valid_to=valid_to)
    if before == after:
        conn.close()
        flash("לא בוצעו שינויים.", "success")
        return _back_to_rule(constitution_id, rule_id)

    periods = [dict(p, **after) if p["param_id"] == param_id else p
               for p in _param_periods(conn, rule_id, param["param_name"])]
    error = _check_periods(periods)
    if error:
        conn.close()
        flash(error, "error")
        return _back_to_rule(constitution_id, rule_id)

    conn.execute("UPDATE rule_parameters SET param_value=%s, valid_from=%s, valid_to=%s WHERE param_id=%s",
                 (value, valid_from, valid_to, param_id))
    log_change(conn, constitution_id, "parameter", "update", before, after,
               request.form.get("reason"), rule_id=rule_id)
    conn.commit()
    conn.close()
    flash(f"הפרמטר {param['param_name']} עודכן.", "success")
    return _back_to_rule(constitution_id, rule_id)


@bp.route("/<int:constitution_id>/rules/<rule_id>/params/new", methods=["POST"])
def add_param_period(constitution_id, rule_id):
    """
    הוספת תקופת תוקף חדשה לפרמטר קיים (למשל: 1.4% ליחידה החל מ-1.9.2026).
    שם הפרמטר חייב להיות אחד שכבר קיים לחוק - רק אותו engine.py קורא.
    אם התקופה החדשה פתוחה (ללא תאריך סיום), התקופה הפתוחה שקדמה לה נסגרת
    אוטומטית ביום תחילת התקופה החדשה.
    """
    conn = get_connection()
    rule, resp = _rule_or_redirect(conn, constitution_id, rule_id)
    if resp:
        return resp
    param_name = request.form.get("param_name") or ""
    periods = _param_periods(conn, rule_id, param_name)
    if not periods:
        conn.close()
        flash("שם פרמטר לא מוכר לחוק הזה.", "error")
        return _back_to_rule(constitution_id, rule_id)
    try:
        value, valid_from, valid_to = _parse_param_form(request.form)
    except ValueError as exc:
        conn.close()
        flash(str(exc), "error")
        return _back_to_rule(constitution_id, rule_id)
    if not valid_from:
        conn.close()
        flash("לתקופה חדשה חובה תאריך תחילת תוקף.", "error")
        return _back_to_rule(constitution_id, rule_id)

    new_period = dict(param_id=None, param_name=param_name, param_value=value,
                      valid_from=valid_from, valid_to=valid_to)
    trimmed = []
    updated = []
    for p in periods:
        p = dict(p)
        covers_start = (p["valid_from"] or "") < valid_from and (p["valid_to"] is None or p["valid_to"] > valid_from)
        if valid_to is None and covers_start and p["valid_to"] is None:
            trimmed.append((_row_snapshot(p, PARAM_FIELDS), p["param_id"]))
            p["valid_to"] = valid_from
        updated.append(p)
    error = _check_periods(updated + [new_period])
    if error:
        conn.close()
        flash(error, "error")
        return _back_to_rule(constitution_id, rule_id)

    reason = request.form.get("reason")
    for before, pid in trimmed:
        conn.execute("UPDATE rule_parameters SET valid_to=%s WHERE param_id=%s", (valid_from, pid))
        log_change(conn, constitution_id, "parameter", "update", before, dict(before, valid_to=valid_from),
                   reason, rule_id=rule_id)
    conn.execute(
        "INSERT INTO rule_parameters (rule_id, param_name, param_value, valid_from, valid_to) "
        "VALUES (%s, %s, %s, %s, %s)",
        (rule_id, param_name, value, valid_from, valid_to),
    )
    log_change(conn, constitution_id, "parameter", "insert", None,
               {k: new_period[k] for k in PARAM_FIELDS}, reason, rule_id=rule_id)
    conn.commit()
    conn.close()
    msg = f"נוספה תקופה חדשה לפרמטר {param_name}."
    if trimmed:
        msg += f" התקופה הקודמת נסגרה ב-{valid_from}."
    flash(msg, "success")
    return _back_to_rule(constitution_id, rule_id)


@bp.route("/<int:constitution_id>/rules/<rule_id>/params/<int:param_id>/delete", methods=["POST"])
def delete_param(constitution_id, rule_id, param_id):
    conn = get_connection()
    rule, resp = _rule_or_redirect(conn, constitution_id, rule_id)
    if resp:
        return resp
    param = conn.execute(
        "SELECT * FROM rule_parameters WHERE param_id=%s AND rule_id=%s", (param_id, rule_id)
    ).fetchone()
    if not param:
        conn.close()
        flash("פרמטר לא נמצא.", "error")
        return _back_to_rule(constitution_id, rule_id)
    if len(_param_periods(conn, rule_id, param["param_name"])) <= 1:
        conn.close()
        flash("אי אפשר למחוק את התקופה היחידה של פרמטר - החישוב נשען עליו. אפשר לערוך את הערך שלה.", "error")
        return _back_to_rule(constitution_id, rule_id)
    conn.execute("DELETE FROM rule_parameters WHERE param_id=%s", (param_id,))
    log_change(conn, constitution_id, "parameter", "delete", _row_snapshot(param, PARAM_FIELDS), None,
               request.form.get("reason"), rule_id=rule_id)
    conn.commit()
    conn.close()
    flash(f"תקופה של הפרמטר {param['param_name']} נמחקה.", "success")
    return _back_to_rule(constitution_id, rule_id)


# ------------------------------------------------------------------
# יומן שינויים
# ------------------------------------------------------------------

def _log_entries(rows):
    entries = []
    for r in rows:
        before = json.loads(r["before_json"]) if r["before_json"] else None
        after = json.loads(r["after_json"]) if r["after_json"] else None
        entries.append(dict(
            r,
            entity_label=ENTITY_LABELS.get(r["entity"], r["entity"]),
            action_label=ACTION_LABELS.get(r["action"], r["action"]),
            param_name=(after or before or {}).get("param_name"),
            changes=_changed_fields(before, after),
        ))
    return entries


@bp.route("/<int:constitution_id>/log")
def constitution_log(constitution_id):
    conn = get_connection()
    constitution = _get_constitution(conn, constitution_id)
    if not constitution:
        conn.close()
        flash("חוקה לא נמצאה.", "error")
        return redirect(url_for("backoffice.constitutions"))
    rows = conn.execute(
        "SELECT * FROM constitution_change_log WHERE constitution_id=%s ORDER BY change_id DESC",
        (constitution_id,),
    ).fetchall()
    conn.close()
    return render_template("constitution_log.html", constitution=constitution, log=_log_entries(rows))
