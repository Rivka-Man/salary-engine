"""
אוצר המילים של החוקה הטבלאית: השדות שאפשר לבנות מהם תנאי, פעולות ההשוואה,
והפעולות שחוק יכול לבצע. כל עמודה בטבלת החוקה נבחרת מרשימה מכאן - אין טקסט
חופשי בתנאים.

בשלב הזה הטבלה מתעדת את הלוגיקה בצורה מובנית. המנוע (engine.py) עדיין מחשב
לפי הקוד, ולא קורא את התנאים מכאן.
"""
import re

# שדה -> (תווית, סוג, קבוצה). סוגים: bool / date / number / choice / text.
FIELDS = {
    # מורה
    "in_reform": ("ברפורמה (עוז לתמורה)", "bool", "מורה"),
    "is_pensioner": ("גמלאי", "bool", "מורה"),
    "reform_start_date": ("תאריך הצטרפות לעוז לתמורה", "date", "מורה"),
    "u_pre": ("יחידות ערב הרפורמה", "number", "מורה"),
    "h_pre": ("שעות ערב הרפורמה", "number", "מורה"),
    "has_double_degree_gimul": ("גמול כפל תואר", "bool", "מורה"),
    "has_additional_double_degree_gimul": ("כפל תואר נוסף", "bool", "מורה"),
    "has_beinayim_gimul": ("גמול ביניים", "bool", "מורה"),
    "dual_reform_ofek_hadash": ("מועסק גם באופק חדש", "bool", "מורה"),
    "certification_date": ("תאריך הסמכה", "date", "מורה"),
    # קורס
    "course.start_date": ("תאריך תחילת קורס", "date", "קורס"),
    "course.hours": ("שעות הקורס", "number", "קורס"),
    "course.grade": ("ציון הקורס", "number", "קורס"),
    "course.pre_approved": ("אישור מוקדם לקורס", "bool", "קורס"),
    "course.source_type": ("סוג מוסד הקורס", "choice", "קורס"),
    "course.is_advanced_degree_track": ("מסלול תואר מתקדם", "bool", "קורס"),
    "course.counts_for_oz": ("משתתף בגמולי עוז לתמורה", "bool", "קורס"),
    # שנה
    "year.status": ("סטטוס העסקה בשנה", "choice", "שנה"),
    # חישוב
    "calc_date": ("תאריך חישוב", "date", "חישוב"),
}

CHOICES = {
    "course.source_type": {"institutional": "מוסדי", "school_based": "השתלמות בית-ספרית"},
    "year.status": {"רגיל": "רגיל", "שבתון": "שבתון", "חצי-שבתון": "חצי-שבתון", "חלת": "חל\"ת"},
}

# פעולת השוואה -> (תווית, האם צריך ערך)
OPERATORS = {
    "eq": ("=", True),
    "ne": ("≠", True),
    "lt": ("<", True),
    "le": ("≤", True),
    "gt": (">", True),
    "ge": ("≥", True),
    "in": ("אחד מ-", True),
    "set": ("יש ערך", False),
    "unset": ("אין ערך", False),
}

CONNECTORS = {"AND": "וגם", "OR": "או"}

# פעולות שחוק מבצע, לפי קבוצות לתצוגה ברשימת הבחירה
ACTIONS = {
    "יחידות ושעות": {
        "unit_hours": "קבע שעות ליחידת גמול",
        "graded_fraction": "קבע חלק מינימלי של שעות עם ציון",
        "add_units": "הוסף יחידות גמול",
        "transition_unit": "הוסף יחידת מעבר",
        "compute_pre_reform": "חשב יחידות ערב הרפורמה מהקורסים",
    },
    "תקרות": {
        "units_cap": "קבע תקרת יחידות",
        "annual_cap": "קבע תקרת יחידות לשנה",
        "no_annual_cap": "בטל תקרת יחידות לשנה",
        "school_hours_cap": "קבע תקרת שעות בית-ספרית לשנה",
        "total_cap": "קבע תקרה כוללת",
        "no_total_cap": "ללא תקרה כוללת",
        "min_course_start": "הכר קורסים ליחידה רק מתאריך",
    },
    "קורסים": {
        "exclude_course": "אל תספור את הקורס",
        "pre_reform_course": "ספור בחישוב ערב הרפורמה בלבד",
        "separate_track": "צבור במסלול נפרד",
        "dual_recognition": "הכר במקביל בשתי רפורמות",
    },
    "שכר": {
        "pct_rate": "קבע אחוז תוספת ליחידה",
    },
    "כללי": {
        "precedence": "קבע עדיפות להסכם",
        "other": "אחר (מתואר בתיאור)",
    },
}
ACTION_LABELS = {code: label for group in ACTIONS.values() for code, label in group.items()}


# ------------------------------------------------------------------
# נרמול ותצוגה של ערכים
# ------------------------------------------------------------------

def _norm_one(field, raw):
    """ממיר ערך שהוקלד לצורה השמורה. מעלה ValueError עם הודעה בעברית."""
    label, ftype, _ = FIELDS[field]
    raw = (raw or "").strip()
    if not raw:
        raise ValueError(f"חסר ערך לתנאי על \"{label}\".")
    if ftype == "bool":
        if raw in ("כן", "1", "true", "True"):
            return "1"
        if raw in ("לא", "0", "false", "False"):
            return "0"
        raise ValueError(f"ל\"{label}\" הערך חייב להיות כן או לא.")
    if ftype == "date":
        m = re.match(r"^(\d{1,2})[/.](\d{1,2})[/.](\d{4})$", raw)
        if m:
            return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
            return raw
        raise ValueError(f"ל\"{label}\" הערך חייב להיות תאריך (DD/MM/YYYY).")
    if ftype == "number":
        try:
            float(raw)
        except ValueError:
            raise ValueError(f"ל\"{label}\" הערך חייב להיות מספר.")
        return raw
    if ftype == "choice":
        options = CHOICES[field]
        for code, opt_label in options.items():
            if raw in (code, opt_label):
                return code
        raise ValueError(f"ל\"{label}\" הערך חייב להיות אחד מ: {', '.join(options.values())}.")
    return raw


def normalize_value(field, operator, raw):
    if not OPERATORS[operator][1]:
        return None
    if operator == "in":
        parts = [p for p in re.split(r"[,،]", raw or "") if p.strip()]
        if not parts:
            raise ValueError(f"חסרים ערכים לתנאי \"אחד מ-\" על \"{FIELDS[field][0]}\".")
        return ",".join(_norm_one(field, p) for p in parts)
    return _norm_one(field, raw)


def _show_one(field, value):
    _, ftype, _ = FIELDS[field]
    if ftype == "bool":
        return "כן" if value == "1" else "לא"
    if ftype == "date" and re.match(r"^\d{4}-\d{2}-\d{2}$", value or ""):
        y, m, d = value.split("-")
        return f"{d}/{m}/{y}"
    if ftype == "choice":
        return CHOICES[field].get(value, value)
    return value


def show_value(cond):
    """הערך של תנאי לתצוגה: שדה אחר, רשימה, או ערך בודד."""
    if cond.get("value_field"):
        return FIELDS.get(cond["value_field"], (cond["value_field"],))[0]
    if cond.get("value") is None:
        return ""
    if cond["operator"] == "in":
        return ", ".join(_show_one(cond["field"], v) for v in cond["value"].split(","))
    return _show_one(cond["field"], cond["value"])


def edit_value(cond):
    """הערך כפי שמוצג בשדה העריכה (אותו פורמט שהמשתמשת מקלידה)."""
    if cond.get("value_field") or cond.get("value") is None:
        return ""
    return show_value(cond)


def describe_conditions(conds):
    """"ברפורמה = כן וגם תאריך חישוב ≥ 01/09/2025". ללא תנאים: "תמיד"."""
    if not conds:
        return "תמיד"
    parts = []
    for i, c in enumerate(conds):
        text = f"{FIELDS.get(c['field'], (c['field'],))[0]} {OPERATORS.get(c['operator'], (c['operator'],))[0]}"
        val = show_value(c)
        if val:
            text += f" {val}"
        if i:
            text = f"{CONNECTORS.get(c['connector'], c['connector'])} {text}"
        parts.append(text)
    return " ".join(parts)


def condition_chips(conds):
    """התנאים כרשימת (מחבר, טקסט) - לתצוגת "אם [..] וגם [..]" בבקאופיס. ללא תנאים: [(None, "תמיד")]."""
    if not conds:
        return [(None, "תמיד")]
    out = []
    for i, c in enumerate(conds):
        text = f"{FIELDS.get(c['field'], (c['field'],))[0]} {OPERATORS.get(c['operator'], (c['operator'],))[0]}"
        val = show_value(c)
        if val:
            text += f" {val}"
        out.append((CONNECTORS.get(c["connector"], c["connector"]) if i else None, text))
    return out


# סוג הפעולה לצבע בתצוגת החוקים: עדיפות / יחידות / תקרה / תוספת שכר / פסילה
ACTION_KIND = {
    "precedence": "violet",
    "unit_hours": "accent", "graded_fraction": "accent", "add_units": "accent",
    "transition_unit": "accent", "compute_pre_reform": "accent",
    "units_cap": "warn", "annual_cap": "warn", "no_annual_cap": "warn", "school_hours_cap": "warn",
    "total_cap": "warn", "no_total_cap": "warn", "min_course_start": "warn",
    "exclude_course": "critical", "pre_reform_course": "neutral", "separate_track": "neutral",
    "dual_recognition": "neutral",
    "pct_rate": "good",
}


# ------------------------------------------------------------------
# זריעה: החוקים הקיימים כשורות בטבלה. נזרע פעם אחת לכל חוק (rules.logic_seeded),
# כך שעריכות מהבקאופיס לא נדרסות.
# תנאי = (מחבר, שדה, השוואה, ערך, שדה-להשוואה)
# ------------------------------------------------------------------

def _c(field, op, value=None, connector="AND", value_field=None):
    return dict(connector=connector, field=field, operator=op, value=value, value_field=value_field)


RULE_LOGIC_SEED = {
    "R21_PRIORITY_OVERRIDE": ([_c("in_reform", "eq", "1")], "precedence", "הסכם עוז לתמורה גובר על החוזר"),
    "R01_UNIT_HOURS": ([], "unit_hours", None),
    "R01B_GRADED_COMPOSITION": ([], "graded_fraction", None),
    "R15_PENSIONER_RATE": ([_c("is_pensioner", "eq", "1")], "unit_hours", None),
    "R17_NO_PREAPPROVAL": ([_c("course.pre_approved", "eq", "0")], "exclude_course", None),
    "R08B_PRE_REFORM_EXCLUDED": (
        [_c("course.start_date", "lt", value_field="reform_start_date"),
         _c("course.counts_for_oz", "eq", "0", connector="OR")],
        "pre_reform_course", None),
    "R08C_PRE_REFORM_COMPUTED": (
        [_c("in_reform", "eq", "1"), _c("u_pre", "eq", "0"), _c("h_pre", "eq", "0")],
        "compute_pre_reform", "תקרה 16, בלי תקרה שנתית"),
    "R02_BASE_CAP": ([_c("in_reform", "eq", "0")], "units_cap", None),
    "R03_REFORM_CAP": ([_c("in_reform", "eq", "1")], "units_cap", None),
    "R08_TRANSITION_UNIT": ([_c("in_reform", "eq", "1"), _c("u_pre", "eq", "16")], "transition_unit", "יחידה 17"),
    "R04_DOUBLE_DEGREE": ([_c("has_double_degree_gimul", "eq", "1")], "add_units", None),
    "R05_BEINAYIM": ([_c("has_beinayim_gimul", "eq", "1"), _c("has_double_degree_gimul", "eq", "0")],
                     "add_units", None),
    "R04B_ADDITIONAL_DOUBLE_DEGREE": (
        [_c("has_additional_double_degree_gimul", "eq", "1"), _c("has_double_degree_gimul", "eq", "1")],
        "add_units", None),
    "R09_THRESH_18": ([_c("in_reform", "eq", "1")], "min_course_start",
                      "יחידה 18: מתשע\"ב או מההצטרפות, המאוחר מביניהם"),
    "R10_THRESH_19": ([_c("in_reform", "eq", "1")], "min_course_start",
                      "יחידה 19: מתשע\"ט או מההצטרפות, המאוחר מביניהם, ורק מתש\"פ"),
    "R13_SCHOOL_HOURS_CAP": ([_c("course.source_type", "eq", "school_based")], "school_hours_cap", None),
    "R14_ADVANCED_DEGREE": ([_c("course.is_advanced_degree_track", "eq", "1")], "separate_track",
                            "גמול אחד בשנה, בלי תקרה"),
    "R11_ANNUAL_CAP": ([], "annual_cap", None),
    "R12_SABBATICAL_EXEMPT": ([_c("year.status", "in", "שבתון,חצי-שבתון,חלת")], "no_annual_cap", None),
    "R06_TOTAL_MAX": ([_c("in_reform", "eq", "0")], "total_cap", None),
    "R06B_NO_CAP_REFORM": ([_c("in_reform", "eq", "1")], "no_total_cap", None),
    "R07_PCT_RATE": ([_c("in_reform", "eq", "0"), _c("calc_date", "lt", "2025-09-01", connector="OR")],
                     "pct_rate", None),
    "R07B_PCT_RATE_REFORM": ([_c("in_reform", "eq", "1"), _c("calc_date", "ge", "2025-09-01")],
                             "pct_rate", None),
    "R16_DUAL_REFORM": ([_c("dual_reform_ofek_hadash", "eq", "1")], "dual_recognition", None),
}


def seed_rule_logic(conn):
    """ממלא תנאים/פעולה/תוצאה לחוקים שעוד לא נזרעו. לא נוגע בחוק שכבר נזרע או נערך."""
    pending = {r["rule_id"] for r in conn.execute(
        "SELECT rule_id FROM rules WHERE COALESCE(logic_seeded, 0) = 0").fetchall()}
    for rule_id, (conds, action, result) in RULE_LOGIC_SEED.items():
        if rule_id not in pending:
            continue
        for seq, c in enumerate(conds, start=1):
            conn.execute(
                "INSERT INTO rule_conditions (rule_id, seq, connector, field, operator, value, value_field) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (rule_id, seq, c["connector"], c["field"], c["operator"], c["value"], c["value_field"]),
            )
        conn.execute("UPDATE rules SET action=%s, result_value=%s, logic_seeded=1 WHERE rule_id=%s",
                     (action, result, rule_id))
