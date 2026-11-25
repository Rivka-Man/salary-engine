"""
מאגר הפעולות (playbook): "מה עליי לעשות?" אחרי שנמצא פער בתלוש.

ההמלצה לא נכתבת חופשית: היא נגזרת באופן דטרמיניסטי מתוצאת ההשוואה
(payslips.compare) ומ"מקור הנתון" של כל רכיב שחסר בתלוש:

  A - הנתון מוכר במשרד, אבל לא הגיע לתלוש     -> חשבות השכר + חברת יוניק (מפיקת התלושים)
  B - השתלמות שהוזנה ידנית ולא דווחה למשרד     -> מוסד ההשתלמות + משרד החינוך, במקביל
  C - חסר מסמך שמוכיח את הזכאות                -> להשיג את המסמך ולטעון אותו
  D - נתון חסר או שגוי בתיק שלנו               -> לתקן את הנתונים ולחשב שוב
  E - לא ברור                                  -> מוקד חשבות השכר *6552

כל סיבה מגדירה "מסלולים" (tracks): למי פונים, באיזה ערוץ, אילו צעדים ומה
מצרפים, ונוסח פנייה מוכן. מסלולים של אותה סיבה רצים במקביל.
מספר ת"ז לא נשמר במערכת: בנוסח מופיע [ת"ז — להשלים] והמורה משלימה בעצמה.
"""
from payslips import CONTACT, _fmt, _units, month_label

ID_PLACEHOLDER = '[ת"ז — להשלים]'

# מספר המוקד בתוך משפט בעברית: עטוף בבידוד כיווניות, כדי שהכוכבית תוצג לפני המספר
PHONE_TXT = "\u2066*6552\u2069"

# פרטי הקשר של יוניק עוד לא אומתו - מוצגים כמקום להשלמה
UNIQUE_CONTACT = "[פרטי קשר של יוניק — להשלים]"

CAUSES = {
    "A": {
        "code": "A", "title": "הנתון מוכר במשרד, אבל לא הגיע לתלוש",
        "short": "חסר בתלוש בלבד",
        "when": "הרכיב החסר מבוסס על נתון שכבר נמצא במשרד: יחידות ערב הרפורמה, יחידת המעבר, "
                "כפל תואר, או השתלמויות שנקלטו מקובץ \"הקורסים שלי\" בפורטל.",
        "tracks": ["payroll", "unique"],
    },
    "B": {
        "code": "B", "title": "השתלמות שלא דווחה למשרד החינוך",
        "short": "קורס שהוזן ידנית",
        "when": "הרכיב החסר נשען על קורס שהמורה הזינה ידנית ולא מופיע ב\"הקורסים שלי\" בפורטל. "
                "כנראה מוסד ההשתלמות לא דיווח עליו, או שהדיווח לא נקלט במשרד.",
        "tracks": ["institution", "ministry"],
    },
    "C": {
        "code": "C", "title": "חסר מסמך שמוכיח את הזכאות",
        "short": "חסר מסמך",
        "when": "הזכאות תלויה במסמך שלא נטען למערכת (למשל תעודות תואר לכפל תואר).",
        "tracks": ["document"],
    },
    "D": {
        "code": "D", "title": "נתון חסר או שגוי בתיק",
        "short": "לתקן נתונים",
        "when": "בתלוש מוכרות יותר יחידות ממה שחישבנו: כנראה חסרים אצלנו קורסים או יחידות ערב הרפורמה.",
        "tracks": ["fix_data"],
    },
    "E": {
        "code": "E", "title": "לא ברור ממה נובע הפער",
        "short": "לברר במוקד",
        "when": "הפער לא מתפרק לרכיבים מוכרים. צריך בירור מול חשבות השכר.",
        "tracks": ["hotline"],
    },
}

TRACKS = {
    "payroll": {"who": "חשבות השכר במשרד החינוך", "channel": CONTACT["portal_label"],
                "link": CONTACT["portal"], "phone": CONTACT["phone"]},
    "unique": {"who": "יוניק — החברה שמפיקה את התלושים", "channel": UNIQUE_CONTACT,
               "link": None, "phone": None},
    "institution": {"who": "מוסד ההשתלמות", "channel": "מייל למזכירות המוסד", "link": None, "phone": None},
    "ministry": {"who": "משרד החינוך — אגף השתלמויות", "channel": CONTACT["portal_label"],
                 "link": CONTACT["portal"], "phone": CONTACT["phone"]},
    "document": {"who": "את/ה", "channel": "טעינה ב-OptiPay", "link": None, "phone": None},
    "fix_data": {"who": "את/ה", "channel": "עדכון הנתונים ב-OptiPay", "link": None, "phone": None},
    "hotline": {"who": "מוקד חשבות השכר", "channel": "טלפון", "link": None, "phone": CONTACT["phone"]},
}


def _course_year(c):
    """שנה"ל של הקורס: מה שנשמר, ואם לא נשמר - לפי תאריך ההתחלה, כמו במנוע."""
    if c.get("school_year"):
        return c["school_year"]
    from engine import school_year_of
    try:
        return school_year_of(c.get("start_date")) if c.get("start_date") else None
    except Exception:
        return None


def _year_courses(courses, school_year):
    return [c for c in courses if _course_year(c) == school_year]


def classify(component, courses):
    """הסיבה לרכיב אחד שחסר בתלוש, לפי מקור הנתון שלו."""
    key = component["key"]
    if key.startswith("year:"):
        yc = _year_courses(courses, component.get("school_year"))
        if any(c.get("source") == "manual" for c in yc):
            return "B"
    return "A"


def plan(cmp, courses, teacher, slip):
    """
    cmp - פלט payslips.compare. courses - הקורסים של המורה (עם source).
    מחזיר: {"ok", "causes": [קודים], "items": [רכיב + סיבה], "tracks": [...], "recheck": bool}
    """
    if cmp["ok"]:
        return {"ok": True, "causes": [], "items": [], "tracks": [], "recheck": False}
    kind = cmp["cause"]["kind"]
    name = (teacher.get("full_name") if hasattr(teacher, "get") else teacher["full_name"]) or ""
    month = month_label(slip["pay_month"])

    if kind == "overpaid":
        codes, items = ["D"], []
    else:
        flagged = [c for c in cmp["components"] if c.get("flagged")]
        items = [dict(c, cause=classify(c, courses)) for c in flagged]
        codes = sorted({i["cause"] for i in items}) or ["E"]

    tracks = []
    for code in codes:
        mine = [i for i in items if i["cause"] == code]
        for t in CAUSES[code]["tracks"]:
            tracks.append(_track(t, code, mine, cmp, courses, name, month, slip))
    return {
        "ok": False, "causes": codes, "cause_defs": [CAUSES[c] for c in codes],
        "items": items, "tracks": tracks,
        "parallel": len(tracks) > 1,
        "recheck": "B" in codes,
    }


def _dmy(iso):
    """'2024-10-02' -> '02/10/2024'. ערך אחר עובר כמו שהוא."""
    s = str(iso or "")
    return f"{s[8:10]}/{s[5:7]}/{s[:4]}" if len(s) >= 10 and s[4] == "-" and s[7] == "-" else s


def _sum(items):
    return sum(i["units"] for i in items)


def _track(t, code, items, cmp, courses, name, month, slip):
    base = dict(TRACKS[t], id=t, cause=code)
    units = _sum(items) if items else cmp["gap_units"]
    what = ", ".join(f"{i['label']} ({_fmt(i['units'])} יח')" for i in items) or f"{_units(units)}"
    sign = ["", "תודה,", name, ID_PLACEHOLDER]

    if t == "payroll":
        base["steps"] = ["להעתיק את נוסח הפנייה ולהשלים מספר ת\"ז",
                         "לשלוח בפניות המקוונות בפורטל עובדי הוראה, ולצרף את המסמכים",
                         "לשמור את מספר הפנייה. אם אין מענה תוך 30 יום: מוקד " + PHONE_TXT]
        base["attach"] = cmp["cause"].get("attach") or ["צילום התלוש"]
        base["letter"] = "\n".join(
            ["שלום רב,", "",
             f"בתלוש לחודש {month} " + ("לא מופיע גמול השתלמות עוז לתמורה." if cmp["line_missing"] else
                                         f"מוכרות בגמול השתלמות עוז לתמורה {_units(cmp['paid_units'])}."),
             f"לפי הנתונים שכבר נמצאים במשרד אני זכאית ל-{_units(cmp['expected_units'])}. "
             f"חסרים: {what}."]
            + ([f"הבסיס: {cmp['cause']['rule_ref']}."] if cmp["cause"].get("rule_ref") else [])
            + ["", "אבקש לעדכן את הגמול ולשלם הפרשים מהמועד שבו נוצרה הזכאות."]
            + sign)
    elif t == "unique":
        base["steps"] = ["רק אם בפורטל עובדי הוראה הנתון מופיע נכון והתלוש לא",
                         "לפנות ליוניק עם צילום התלוש ופירוט השורה השגויה",
                         "לעדכן את חשבות השכר שנפתחה פנייה גם ליוניק"]
        base["attach"] = ["צילום התלוש", "צילום מסך מהפורטל שבו הנתון מופיע נכון"]
        base["letter"] = "\n".join(
            ["שלום רב,", "",
             f"בתלוש השכר שלי לחודש {month}, בשורה {slip['gimul_code'] or '249'} "
             f"(\"{slip['gimul_desc'] or 'ג. השת. עוז לתמורה'}\"), "
             + ("השורה לא מופיעה." if cmp["line_missing"] else f"מופיעות {_fmt(cmp['paid_units'])} יחידות."),
             f"בנתוני משרד החינוך מוכרות לי {_fmt(cmp['expected_units'])} יחידות. נראה שהפער נוצר בהפקת התלוש.",
             "", "אבקש לבדוק ולתקן."] + sign)
    elif t == "institution":
        cs = [c for i in items for c in _year_courses(courses, i.get("school_year")) if c.get("source") == "manual"]
        lines = [f"- {c.get('description') or 'קורס'}, {_dmy(c.get('start_date'))}"
                 f"{' עד ' + _dmy(c['end_date']) if c.get('end_date') else ''}, {_fmt(c.get('hours'))} שעות" for c in cs]
        base["steps"] = ["לשלוח את המייל למזכירות המוסד שבו נלמד הקורס",
                         "לבקש אישור שהדיווח נשלח למשרד החינוך",
                         "אחרי כשבועיים: לבדוק ב\"הקורסים שלי\" בפורטל שהקורס מופיע"]
        base["attach"] = ["תעודת סיום / אישור השתתפות בקורס"]
        base["courses"] = cs
        base["letter"] = "\n".join(
            ["שלום רב,", "",
             "השתתפתי בהשתלמות במוסד שלכם, והיא לא מופיעה ברשימת \"הקורסים שלי\" בפורטל עובדי הוראה של משרד החינוך:"]
            + lines
            + ["", "אבקש לדווח על ההשתלמות למשרד החינוך (או לוודא שהדיווח נקלט), ולשלוח לי אישור על הדיווח."]
            + sign)
    elif t == "ministry":
        base["steps"] = ["במקביל למייל למוסד: פנייה מקוונת בפורטל",
                         "לצרף את תעודת הסיום",
                         "כשהקורס יופיע בפורטל: לטעון שוב את \"הקורסים שלי\" ולחשב מחדש"]
        base["attach"] = ["תעודת סיום / אישור השתתפות בקורס", "צילום התלוש"]
        base["letter"] = "\n".join(
            ["שלום רב,", "",
             f"השלמתי השתלמות שלא מופיעה ב\"הקורסים שלי\" בפורטל, ולכן היחידות שלה ({_fmt(units)} יח') "
             f"לא נכללו בגמול השתלמות עוז לתמורה בתלוש {month}.",
             "פניתי במקביל למוסד ההשתלמות בבקשה לדווח עליה.",
             "", "אבקש לקלוט את ההשתלמות ולעדכן את הגמול, כולל הפרשים."] + sign)
    elif t == "document":
        base["steps"] = ["להשיג את המסמך (פורטל עובדי הוראה / המוסד האקדמי)",
                         "לטעון אותו ב-OptiPay", "לחשב שוב"]
        base["attach"] = []
        base["letter"] = ""
    elif t == "fix_data":
        base["steps"] = ["לוודא שכל הקורסים מ\"הקורסים שלי\" בפורטל נטענו",
                         "לוודא שהוזנו יחידות ערב הרפורמה", "לחשב שוב"]
        base["attach"] = []
        base["letter"] = ""
    else:  # hotline
        base["steps"] = ["להתקשר למוקד " + PHONE_TXT, "להקריא את הפער ואת שורת הגמול בתלוש",
                         "לרשום את מספר הפנייה"]
        base["attach"] = ["צילום התלוש"]
        base["letter"] = ""
    return base
