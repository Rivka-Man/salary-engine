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
from rule_vocab import (ACTION_LABELS as RULE_ACTION_LABELS, ACTIONS, CHOICES, CONNECTORS, FIELDS, OPERATORS, describe_conditions, condition_chips, ACTION_KIND, show_value,
                        edit_value, normalize_value)

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
LOGIC_FIELD_LABELS = {"conditions": "חל על", "action": "פעולה", "result_value": "תוצאה",
                      "rule_id": "קוד", "priority": "סדר"}
FIELD_LABELS = dict(RULE_EDITABLE_FIELDS + CONSTITUTION_EDITABLE_FIELDS, **PARAM_FIELD_LABELS,
                    **LOGIC_FIELD_LABELS)

# כמה שורות ריקות להוספת תנאים מוצגות בטופס העריכה של חוק
NEW_CONDITION_ROWS = 3

ENTITY_LABELS = {"constitution": "חוקה", "rule": "חוק", "parameter": "פרמטר"}
ACTION_LABELS = {"insert": "הוספה", "update": "עדכון", "delete": "מחיקה"}
SOURCE_LABELS = {"manual": "ידני", "agreement": "הסכם", "restore": "שחזור"}


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


def log_change(conn, constitution_id, entity, action, before, after, reason, rule_id=None, source="manual"):
    """source: 'manual' (עריכה בבקאופיס) / 'agreement' (החלת הסכם) / 'restore' (שחזור גרסה)."""
    conn.execute(
        "INSERT INTO constitution_change_log "
        "(constitution_id, rule_id, entity, action, before_json, after_json, reason, source, changed_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            constitution_id, rule_id, entity, action,
            json.dumps(before, ensure_ascii=False) if before is not None else None,
            json.dumps(after, ensure_ascii=False) if after is not None else None,
            (reason or "").strip() or None, source, _now(),
        ),
    )


def _get_constitution(conn, constitution_id):
    return conn.execute("SELECT * FROM constitutions WHERE constitution_id=%s", (constitution_id,)).fetchone()


def _get_rule(conn, constitution_id, rule_id):
    return conn.execute(
        "SELECT * FROM rules WHERE rule_id=%s AND constitution_id=%s", (rule_id, constitution_id)
    ).fetchone()


def _conditions_by_rule(conn, rule_ids):
    if not rule_ids:
        return {}
    rows = conn.execute(
        "SELECT * FROM rule_conditions WHERE rule_id = ANY(%s) ORDER BY rule_id, seq, condition_id",
        (list(rule_ids),),
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["rule_id"], []).append(dict(r))
    return out


def _logic_snapshot(rule, conds):
    """מצב הלוגיקה של חוק כטקסט קריא - לשמירה ביומן השינויים."""
    return {
        "conditions": describe_conditions(conds),
        "action": RULE_ACTION_LABELS.get(rule["action"], rule["action"]) if rule["action"] else None,
        "result_value": rule["result_value"],
    }


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
    # ציר הגרסאות לכל חוקה: גרסאות קודמות, הגרסה הנוכחית ותאריך התוקף שלה,
    # והסכמים שנותחו וממתינים לאישור (טיוטה של הגרסה הבאה)
    pending = conn.execute(
        "SELECT agreement_id, title, filename, uploaded_at, proposal_json FROM agreements WHERE status='ready' "
        "ORDER BY agreement_id DESC").fetchall()
    out = []
    for c in rows:
        cid = c["constitution_id"]
        versions = conn.execute(
            "SELECT version_id, created_at, label FROM constitution_versions WHERE constitution_id=%s "
            "ORDER BY created_at DESC, version_id DESC LIMIT 3", (cid,)).fetchall()
        since = conn.execute(
            "SELECT MIN(d) AS d FROM (SELECT valid_from AS d FROM rules WHERE constitution_id=%s "
            "UNION ALL SELECT p.valid_from FROM rule_parameters p JOIN rules r ON r.rule_id=p.rule_id "
            "WHERE r.constitution_id=%s) x WHERE d IS NOT NULL", (cid, cid)).fetchone()["d"]
        latest = conn.execute(
            "SELECT MAX(d) AS d FROM (SELECT valid_from AS d FROM rules WHERE constitution_id=%s "
            "UNION ALL SELECT p.valid_from FROM rule_parameters p JOIN rules r ON r.rule_id=p.rule_id "
            "WHERE r.constitution_id=%s) x WHERE d IS NOT NULL", (cid, cid)).fetchone()["d"]
        drafts = []
        for a in pending:
            try:
                changes = (json.loads(a["proposal_json"] or "{}") or {}).get("changes") or []
            except (ValueError, TypeError):
                changes = []
            mine = [ch for ch in changes if ch.get("constitution_id") in (None, cid)]
            if mine:
                starts = sorted(ch["valid_from"] for ch in mine if ch.get("valid_from"))
                drafts.append({"agreement_id": a["agreement_id"], "title": a["title"] or a["filename"],
                               "changes": len(mine), "valid_from": starts[0] if starts else None})
        out.append(dict(c, versions=list(reversed(versions)), since=since, latest=latest, drafts=drafts))
    conn.close()
    return render_template("constitutions.html", constitutions=out)


@bp.route("/new", methods=["GET", "POST"])
def new_constitution():
    """
    יצירת חוקה חדשה (שם + תיאור). החוקה נוצרת ריקה: החוקים עצמם נוצרים מ-engine.py,
    והמנוע מחשב לפי החוקים של חוקת גמול ההשתלמות, כך שחוקה חדשה לא משפיעה על החישוב.
    """
    form = {f: (request.form.get(f) or "").strip() for f, _ in CONSTITUTION_EDITABLE_FIELDS}
    if request.method == "POST":
        if not form["name"]:
            flash("יש למלא שם לחוקה.", "error")
            return render_template("constitution_new.html", form=form)
        conn = get_connection()
        if conn.execute("SELECT 1 FROM constitutions WHERE name=%s", (form["name"],)).fetchone():
            conn.close()
            flash("כבר קיימת חוקה בשם הזה.", "error")
            return render_template("constitution_new.html", form=form)
        # code הוא מזהה פנימי קבוע (ייחודי, לא מוצג) - נגזר מזמן היצירה.
        code = "custom_" + datetime.now().strftime("%Y%m%d%H%M%S%f")
        after = {"name": form["name"], "description": form["description"] or None}
        row = conn.execute(
            "INSERT INTO constitutions (code, name, description) VALUES (%s, %s, %s) RETURNING constitution_id",
            (code, after["name"], after["description"]),
        ).fetchone()
        log_change(conn, row["constitution_id"], "constitution", "insert", None, after, request.form.get("reason"))
        conn.commit()
        conn.close()
        flash(f"החוקה \"{after['name']}\" נוצרה.", "success")
        return redirect(url_for("backoffice.constitution_detail", constitution_id=row["constitution_id"]))
    return render_template("constitution_new.html", form=form)


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
    conds = _conditions_by_rule(conn, [r["rule_id"] for r in rules])
    conn.close()
    params_by_rule = {}
    for p in params:
        params_by_rule.setdefault(p["rule_id"], []).append(p)
    applies = {r["rule_id"]: describe_conditions(conds.get(r["rule_id"], [])) for r in rules}
    chips = {r["rule_id"]: condition_chips(conds.get(r["rule_id"], [])) for r in rules}
    return render_template(
        "constitution_detail.html", constitution=constitution, rules=rules, params_by_rule=params_by_rule,
        applies=applies, chips=chips, action_kind=ACTION_KIND, action_labels=RULE_ACTION_LABELS,
    )


# כמה החוק תרם לתוצאה, לפי ערך ה-trace שלו (למסך "הופעל על")
_UNIT_RULES = {"R04_DOUBLE_DEGREE", "R04B_ADDITIONAL_DOUBLE_DEGREE", "R05_BEINAYIM"}


def _rule_applied_on(conn, rule_id, limit=6):
    """המורות שהחוק הופעל עליהן בחישוב האחרון שלהן, עם התרומה שלו לתוצאה."""
    rows = conn.execute(
        "SELECT DISTINCT ON (r.teacher_id) r.teacher_id, r.calc_date, r.result_json, t.full_name "
        "FROM calculation_runs r JOIN teachers t ON t.teacher_id=r.teacher_id "
        "ORDER BY r.teacher_id, r.run_id DESC"
    ).fetchall()
    out = []
    for r in rows:
        try:
            trace = json.loads(r["result_json"]).get("trace") or []
        except (ValueError, TypeError):
            continue
        hit = next((t for t in trace if t.get("rule_id") == rule_id), None)
        if not hit:
            continue
        v = hit.get("value")
        if rule_id == "R08_TRANSITION_UNIT":
            effect = "+1"
        elif rule_id in _UNIT_RULES and isinstance(v, (int, float)):
            effect = f"+{v:g}"
        elif rule_id == "R17_NO_PREAPPROVAL":
            effect = "נפסל"
        else:
            effect = f"{v:g}" if isinstance(v, (int, float)) else ""
        out.append({"teacher_id": r["teacher_id"], "name": r["full_name"], "calc_date": r["calc_date"],
                    "effect": effect, "message": hit.get("message")})
    return out[:limit]


def _rule_origin(conn, rule):
    """
    "נוצר מתוך": הסעיף שממנו החוק נגזר. אם החוק נוצר או שונה מהסכם שנותח, מוצגים
    הציטוט והסעיף שה-LLM חילץ; אחרת - המקור והתיאור שבחוק עצמו.
    """
    rows = conn.execute(
        "SELECT agreement_id, title, filename, status, proposal_json FROM agreements "
        "WHERE status IN ('applied', 'ready') AND proposal_json IS NOT NULL ORDER BY agreement_id DESC"
    ).fetchall()
    for a in rows:
        try:
            changes = (json.loads(a["proposal_json"]) or {}).get("changes") or []
        except (ValueError, TypeError):
            continue
        ch = next((c for c in changes if c.get("rule_id") == rule["rule_id"] and c.get("quote")), None)
        if ch:
            return {"from_llm": True, "doc": a["title"] or a["filename"], "section": ch.get("section"),
                    "quote": ch["quote"], "agreement_id": a["agreement_id"], "status": a["status"]}
    return {"from_llm": False, "doc": (rule["source_doc"] or "").replace("_", " "),
            "section": rule["source_ref"], "quote": None}


RULE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


@bp.route("/<int:constitution_id>/rules/new", methods=["POST"])
def new_rule(constitution_id):
    """
    הוספת חוק חדש לחוקה (שורה חדשה בטבלה). אחרי היצירה עוברים לדף החוק
    כדי להגדיר לו תנאים, פעולה ותוצאה.
    """
    conn = get_connection()
    constitution = _get_constitution(conn, constitution_id)
    if not constitution:
        conn.close()
        flash("חוקה לא נמצאה.", "error")
        return redirect(url_for("backoffice.constitutions"))
    back = redirect(url_for("backoffice.constitution_detail", constitution_id=constitution_id))
    f = {k: (request.form.get(k) or "").strip() for k in
         ("rule_id", "title", "description", "source_doc", "source_ref", "priority")}
    if not RULE_ID_RE.match(f["rule_id"]):
        conn.close()
        flash("קוד החוק חייב להתחיל באות אנגלית ולהכיל רק אותיות אנגליות, ספרות וקו תחתון (למשל R30_NEW_RULE).",
              "error")
        return back
    if not f["title"]:
        conn.close()
        flash("יש למלא כותרת לחוק.", "error")
        return back
    try:
        priority = int(f["priority"] or 0)
    except ValueError:
        conn.close()
        flash("סדר חייב להיות מספר שלם.", "error")
        return back
    if conn.execute("SELECT 1 FROM rules WHERE rule_id=%s", (f["rule_id"],)).fetchone():
        conn.close()
        flash(f"כבר קיים חוק עם הקוד {f['rule_id']}.", "error")
        return back
    conn.execute(
        "INSERT INTO rules (rule_id, topic, title, description, source_doc, source_ref, priority, "
        "active, constitution_id, logic_seeded) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, 1)",
        (f["rule_id"], constitution["name"], f["title"], f["description"] or f["title"],
         f["source_doc"] or "—", f["source_ref"] or None, priority, constitution_id),
    )
    log_change(conn, constitution_id, "rule", "insert", None,
               {"rule_id": f["rule_id"], "title": f["title"], "priority": priority},
               request.form.get("reason"), rule_id=f["rule_id"])
    conn.commit()
    conn.close()
    flash(f"החוק {f['rule_id']} נוסף. עכשיו אפשר להגדיר על מי הוא חל ומה הוא עושה.", "success")
    return _back_to_rule(constitution_id, f["rule_id"])


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
    param_groups = {}
    for p in params:
        param_groups.setdefault(p["param_name"], []).append(p)
    gaps = {name: _param_gaps(rows) for name, rows in param_groups.items()}
    conds = _conditions_by_rule(conn, [rule_id]).get(rule_id, [])
    applied_on = _rule_applied_on(conn, rule_id)
    versions = conn.execute(
        "SELECT version_id, created_at, label FROM constitution_versions WHERE constitution_id=%s "
        "ORDER BY created_at DESC, version_id DESC LIMIT 5", (constitution_id,)
    ).fetchall()
    origin = _rule_origin(conn, rule)
    conn.close()
    for c in conds:
        c["edit_value"] = edit_value(c)
        c["show_value"] = show_value(c)
    return render_template(
        "rule_detail.html", constitution=constitution, rule=rule, param_groups=param_groups, gaps=gaps,
        chips=condition_chips(conds), action_kind=ACTION_KIND.get(rule["action"], "neutral"),
        applied_on=applied_on, versions=versions, origin=origin,
        log=_log_entries(log), conditions=conds, applies=describe_conditions(conds),
        new_condition_rows=NEW_CONDITION_ROWS, fields=FIELDS, operators=OPERATORS, connectors=CONNECTORS,
        actions=ACTIONS, action_labels=RULE_ACTION_LABELS,
        # רשימה ולא dict - כדי שסדר האפשרויות יישמר ב-JSON (tojson ממיין מפתחות)
        choices={f: list(opts.items()) for f, opts in CHOICES.items()},
    )


def validate_condition(field, operator, connector, raw_value, value_field):
    """
    בודק ומנרמל תנאי אחד (מהטופס או מהצעת הסכם). מחזיר dict מוכן לשמירה,
    או מעלה ValueError עם הודעה בעברית.
    """
    field = (field or "").strip()
    if field not in FIELDS:
        raise ValueError(f"שדה לא מוכר בתנאי: {field}.")
    operator = operator or "eq"
    if operator not in OPERATORS:
        raise ValueError(f"השוואה לא מוכרת בתנאי: {operator}.")
    if connector not in CONNECTORS:
        connector = "AND"
    value_field = (value_field or "").strip() or None
    if value_field:
        if value_field not in FIELDS:
            raise ValueError(f"שדה ההשוואה לא מוכר: {value_field}.")
        if operator in ("in", "set", "unset"):
            raise ValueError(f"השוואה ל\"{FIELDS[value_field][0]}\" אפשרית רק עם =, ≠, <, ≤, >, ≥.")
        value = None
    else:
        value = normalize_value(field, operator, raw_value)
    return dict(connector=connector, field=field, operator=operator, value=value, value_field=value_field)


def _parse_conditions(form):
    """
    קורא את שורות התנאים מהטופס (cond-<i>-field וכו'). שורה בלי שדה, או
    שסומנה למחיקה, מדולגת. מעלה ValueError עם הודעה בעברית.
    """
    conds = []
    indexes = sorted({int(m.group(1)) for k in form for m in [re.match(r"^cond-(\d+)-field$", k)] if m})
    for i in indexes:
        field = (form.get(f"cond-{i}-field") or "").strip()
        if not field or form.get(f"cond-{i}-delete"):
            continue
        conds.append(validate_condition(field, form.get(f"cond-{i}-operator"), form.get(f"cond-{i}-connector"),
                                        form.get(f"cond-{i}-value"), form.get(f"cond-{i}-value_field")))
    return conds


def save_rule_logic(conn, constitution_id, rule, new_conds, action, result_value, reason, source="manual"):
    """
    מחליף את התנאים/הפעולה/התוצאה של חוק ורושם ביומן. מחזיר (לפני, אחרי),
    או None כשאין שינוי. לא עושה commit.
    """
    rule_id = rule["rule_id"]
    old_conds = _conditions_by_rule(conn, [rule_id]).get(rule_id, [])
    before = _logic_snapshot(rule, old_conds)
    after = _logic_snapshot(dict(rule, action=action, result_value=result_value), new_conds)
    if before == after:
        return None
    conn.execute("DELETE FROM rule_conditions WHERE rule_id=%s", (rule_id,))
    for seq, c in enumerate(new_conds, start=1):
        conn.execute(
            "INSERT INTO rule_conditions (rule_id, seq, connector, field, operator, value, value_field) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (rule_id, seq, c["connector"], c["field"], c["operator"], c["value"], c["value_field"]),
        )
    conn.execute("UPDATE rules SET action=%s, result_value=%s, logic_seeded=1 WHERE rule_id=%s",
                 (action, result_value, rule_id))
    log_change(conn, constitution_id, "rule", "update", before, after, reason, rule_id=rule_id, source=source)
    return before, after


@bp.route("/<int:constitution_id>/rules/<rule_id>/logic", methods=["POST"])
def edit_rule_logic(constitution_id, rule_id):
    """שמירת "חל על" (התנאים), הפעולה והתוצאה של חוק. התנאים מוחלפים כולם בכל שמירה."""
    conn = get_connection()
    rule, resp = _rule_or_redirect(conn, constitution_id, rule_id)
    if resp:
        return resp
    try:
        new_conds = _parse_conditions(request.form)
    except ValueError as exc:
        conn.close()
        flash(str(exc), "error")
        return _back_to_rule(constitution_id, rule_id)
    action = request.form.get("action") or None
    if action and action not in RULE_ACTION_LABELS:
        conn.close()
        flash("פעולה לא מוכרת.", "error")
        return _back_to_rule(constitution_id, rule_id)
    result_value = (request.form.get("result_value") or "").strip() or None

    if save_rule_logic(conn, constitution_id, rule, new_conds, action, result_value,
                       request.form.get("reason")) is None:
        conn.close()
        flash("לא בוצעו שינויים.", "success")
        return _back_to_rule(constitution_id, rule_id)
    conn.commit()
    conn.close()
    flash("ההגדרה של החוק עודכנה.", "success")
    return _back_to_rule(constitution_id, rule_id)


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


def add_param_period_db(conn, constitution_id, rule_id, param_name, value, valid_from, valid_to, reason,
                        source="manual"):
    """
    מוסיף תקופת תוקף לפרמטר קיים ורושם ביומן; לא עושה commit. תקופה פתוחה
    חדשה סוגרת את התקופה הפתוחה שקדמה לה. תקופה עם אותם תאריכים בדיוק כמו
    תקופה קיימת מעדכנת את הערך שלה. מחזיר הודעה, או מעלה ValueError.
    """
    periods = _param_periods(conn, rule_id, param_name)
    if not periods:
        raise ValueError(f"לחוק {rule_id} אין פרמטר בשם {param_name} - המנוע לא יקרא אותו.")
    if not valid_from:
        raise ValueError("לתקופה חדשה חובה תאריך תחילת תוקף.")
    same = [p for p in periods if p["valid_from"] == valid_from and p["valid_to"] == valid_to]
    if same:
        p = same[0]
        if float(p["param_value"]) == float(value):
            raise ValueError(f"לפרמטר {param_name} כבר יש את הערך {value} בתקופה הזו.")
        before = _row_snapshot(p, PARAM_FIELDS)
        conn.execute("UPDATE rule_parameters SET param_value=%s WHERE param_id=%s", (value, p["param_id"]))
        log_change(conn, constitution_id, "parameter", "update", before, dict(before, param_value=value),
                   reason, rule_id=rule_id, source=source)
        return f"הערך של {param_name} בתקופה הזו עודכן."

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
        raise ValueError(error)

    for before, pid in trimmed:
        conn.execute("UPDATE rule_parameters SET valid_to=%s WHERE param_id=%s", (valid_from, pid))
        log_change(conn, constitution_id, "parameter", "update", before, dict(before, valid_to=valid_from),
                   reason, rule_id=rule_id, source=source)
    conn.execute(
        "INSERT INTO rule_parameters (rule_id, param_name, param_value, valid_from, valid_to) "
        "VALUES (%s, %s, %s, %s, %s)",
        (rule_id, param_name, value, valid_from, valid_to),
    )
    log_change(conn, constitution_id, "parameter", "insert", None,
               {k: new_period[k] for k in PARAM_FIELDS}, reason, rule_id=rule_id, source=source)
    msg = f"נוספה תקופה חדשה לפרמטר {param_name}."
    if trimmed:
        msg += f" התקופה הקודמת נסגרה ב-{valid_from}."
    return msg


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
    try:
        msg = add_param_period_db(conn, constitution_id, rule_id, param_name, value, valid_from, valid_to,
                                  request.form.get("reason"))
    except ValueError as exc:
        conn.close()
        flash(str(exc), "error")
        return _back_to_rule(constitution_id, rule_id)
    conn.commit()
    conn.close()
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
            source_label=SOURCE_LABELS.get(r.get("source"), r.get("source")),
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
