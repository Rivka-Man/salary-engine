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
    for i, t in enumerate(tracks, 1):
        t["n"] = i
    lead, followup = _lead(codes, len(tracks))
    return {
        "ok": False, "causes": codes, "cause_defs": [CAUSES[c] for c in codes],
        "items": items, "tracks": tracks,
        "parallel": len(tracks) > 1,
        "recheck": "B" in codes,
        "lead": lead, "followup": followup,
        "steps_count": len(tracks) + (1 if followup else 0),
    }


def _lead(codes, n_tracks):
    """שורת ההסבר בראש מסך "מה עליי לעשות?" והצעד שאחרי שהגורמים השיבו."""
    if "B" in codes:
        lead = "קורס שהזנת בעצמך לא מופיע אצל המשרד. לכן פונים לשני גורמים **במקביל**, כדי לא לחכות."
        follow = {"t": "אחרי ששניהם השיבו",
                  "d": "כשהקורס מופיע ב\"הקורסים שלי\", מעלים את האישור המעודכן והבדיקה רצה מחדש. אם עדיין יש פער, נכין פנייה לחשבות."}
    elif "A" in codes:
        lead = "הנתונים כבר במשרד, והם לא הגיעו לתלוש. לכן פונים לשני גורמים **במקביל**, כדי לא לחכות."
        follow = {"t": "אחרי ששניהם השיבו",
                  "d": "כשמגיע התלוש הבא, מעלים אותו והבדיקה רצה מחדש. אם עדיין יש פער, ממשיכים מול חשבות השכר."}
    elif "D" in codes:
        lead = "בתלוש מוכרות יותר יחידות ממה שחישבנו. כנראה חסרים אצלנו נתונים."
        follow = None
    else:
        lead = "הפער לא מתפרק לרכיבים מוכרים, ולכן מתחילים בבירור טלפוני."
        follow = None
    if n_tracks < 2:
        lead = lead.replace("לשני גורמים **במקביל**, כדי לא לחכות", "לגורם אחד")
    return lead, follow


def _dmy(iso):
    """'2024-10-02' -> '02/10/2024'. ערך אחר עובר כמו שהוא."""
    s = str(iso or "")
    return f"{s[8:10]}/{s[5:7]}/{s[:4]}" if len(s) >= 10 and s[4] == "-" and s[7] == "-" else s


def _sum(items):
    return sum(i["units"] for i in items)


def _step(title, desc=""):
    return {"t": title, "d": desc}


def _track(t, code, items, cmp, courses, name, month, slip):
    """
    מסלול אחד במסך "מה עליי לעשות?": למי פונים (title, sub), צעדים לסימון
    (steps: כותרת + שורת הסבר), מה מצרפים, ונוסח פנייה (letter_to, letter_subject, letter).
    """
    base = dict(TRACKS[t], id=t, cause=code)
    units = _sum(items) if items else cmp["gap_units"]
    what = ", ".join(f"{i['label']} ({_fmt(i['units'])} יח')" for i in items) or f"{_units(units)}"
    hello = ["שלום רב,", f"שמי {name}, ת\"ז {ID_PLACEHOLDER}."]
    sign = ["", "תודה,", name]
    row = f"שורה {slip['gimul_code'] or '249'} (\"{slip['gimul_desc'] or 'ג. השת. עוז לתמורה'}\")"
    base["letter_to"] = base["letter_subject"] = ""

    if t == "payroll":
        base["title"] = "פנייה לחשבות השכר"
        base["sub"] = "פורטל עובדי הוראה · פניות מקוונות"
        base["attach"] = cmp["cause"].get("attach") or ["צילום התלוש"]
        base["steps"] = [
            _step(f"צרפי {len(base['attach'])} מסמכים" if len(base["attach"]) > 1 else "צרפי את צילום התלוש",
                  (", ".join(base["attach"][:-1]) + " ו" + base["attach"][-1] + ".") if len(base["attach"]) > 1 else ""),
            _step("שלחי פנייה מקוונת", "מבקשים לעדכן את הגמול ולשלם הפרשים מהמועד שבו נוצרה הזכאות."),
            _step("שמרי את מספר הפנייה", "אם אין מענה תוך 30 יום: מוקד " + PHONE_TXT + "."),
        ]
        base["letter_to"] = "חשבות השכר"
        base["letter_subject"] = f"גמול השתלמות עוז לתמורה לא עודכן בתלוש {month}"
        base["letter"] = "\n".join(
            hello
            + [f"בתלוש לחודש {month} " + ("לא מופיע גמול השתלמות עוז לתמורה." if cmp["line_missing"] else
                                           f"מוכרות בגמול השתלמות עוז לתמורה {_units(cmp['paid_units'])}."),
               f"לפי הנתונים שכבר נמצאים במשרד אני זכאית ל-{_units(cmp['expected_units'])}. חסרים: {what}."]
            + ([f"הבסיס: {cmp['cause']['rule_ref']}."] if cmp["cause"].get("rule_ref") else [])
            + ["אבקש לעדכן את הגמול ולשלם הפרשים."] + sign)
    elif t == "unique":
        base["title"] = "פנייה ליוניק, במקביל"
        base["sub"] = "החברה שמפיקה את התלושים · " + UNIQUE_CONTACT
        base["attach"] = ["צילום התלוש", "צילום מסך מהפורטל שבו הנתון מופיע נכון"]
        base["steps"] = [
            _step("בדקי בפורטל שהנתון מופיע נכון", "אם בפורטל הוא נכון ובתלוש לא, הפער נוצר בהפקת התלוש."),
            _step("שלחי את הפנייה", "עם צילום התלוש וצילום המסך מהפורטל."),
        ]
        base["letter_to"] = "יוניק — הפקת תלושי שכר"
        base["letter_subject"] = f"{row} בתלוש {month}"
        base["letter"] = "\n".join(
            hello
            + [f"בתלוש השכר שלי לחודש {month}, ב{row}, "
               + ("השורה לא מופיעה." if cmp["line_missing"] else f"מופיעות {_fmt(cmp['paid_units'])} יחידות."),
               f"בנתוני משרד החינוך מוכרות לי {_fmt(cmp['expected_units'])} יחידות. נראה שהפער נוצר בהפקת התלוש.",
               "אבקש לבדוק ולתקן."] + sign)
    elif t == "institution":
        cs = [c for i in items for c in _year_courses(courses, i.get("school_year")) if c.get("source") == "manual"]
        first = cs[0] if cs else {}
        cname = first.get("description") or "[שם הקורס]"
        lines = [f"- {c.get('description') or 'קורס'}, {_dmy(c.get('start_date'))}"
                 f"{' עד ' + _dmy(c['end_date']) if c.get('end_date') else ''}, {_fmt(c.get('hours'))} שעות" for c in cs]
        base["title"] = "מייל למוסד ההשתלמות"
        base["sub"] = "[שם המוסד] · מייל"
        base["attach"] = ["תעודת סיום / אישור השתתפות בקורס"]
        base["courses"] = cs
        base["steps"] = [
            _step("מצאי את אישור הסיום", "התעודה או האישור שקיבלת בסוף הקורס."),
            _step("שלחי את המייל", "מבקשים לדווח על הקורס למשרד, או לאשר שכבר דווח."),
        ]
        base["letter_to"] = "[מוסד ההשתלמות]"
        base["letter_subject"] = f"בקשה לדיווח השתלמות — {cname}"
        base["letter"] = "\n".join(
            hello
            + ["השתתפתי בהשתלמות במוסד שלכם, והיא לא מופיעה ב\"הקורסים שלי\" בפורטל עובדי הוראה:"]
            + lines
            + ["אבקש לבדוק אם דווחה למשרד החינוך, ואם לא, לדווח עליה ולשלוח לי אישור."] + sign)
    elif t == "ministry":
        base["title"] = "פנייה למשרד, במקביל"
        base["sub"] = "פורטל עובדי הוראה · פניות מקוונות"
        base["attach"] = ["תעודת סיום / אישור השתתפות בקורס", "צילום התלוש"]
        base["steps"] = [
            _step("צרפי 2 מסמכים", "אישור סיום הקורס וצילום התלוש."),
            _step("שלחי פנייה מקוונת", "מבקשים לבדוק אם הדיווח לא נקלט, ולעדכן כולל הפרשים."),
        ]
        base["letter_to"] = "חשבות השכר"
        base["letter_subject"] = "השתלמות שלא מופיעה ב\"הקורסים שלי\""
        base["letter"] = "\n".join(
            hello
            + [f"השלמתי השתלמות שלא מופיעה ב\"הקורסים שלי\" בפורטל, ולכן היחידות שלה ({_fmt(units)} יח') "
               f"לא נכללו בגמול השתלמות עוז לתמורה בתלוש {month}.",
               "פניתי במקביל למוסד ההשתלמות ואעביר את אישורם.",
               "אבקש לקלוט את ההשתלמות ולעדכן את הגמול, כולל הפרשים."] + sign)
    elif t == "document":
        base["title"] = "להשיג את המסמך החסר"
        base["sub"] = "פורטל עובדי הוראה / המוסד האקדמי"
        base["attach"] = []
        base["steps"] = [_step("השיגי את המסמך"), _step("טעני אותו ב-OptiPay"), _step("חשבי שוב")]
        base["letter"] = ""
    elif t == "fix_data":
        base["title"] = "לתקן את הנתונים בתיק"
        base["sub"] = "ב-OptiPay"
        base["attach"] = []
        base["steps"] = [_step("ודאי שכל הקורסים מ\"הקורסים שלי\" נטענו"),
                         _step("ודאי שהוזנו יחידות ערב הרפורמה"), _step("חשבי שוב")]
        base["letter"] = ""
    else:  # hotline
        base["title"] = "בירור במוקד חשבות השכר"
        base["sub"] = "טלפון · " + PHONE_TXT
        base["attach"] = ["צילום התלוש"]
        base["steps"] = [_step("התקשרי למוקד " + PHONE_TXT), _step("הקריאי את הפער ואת שורת הגמול בתלוש"),
                         _step("רשמי את מספר הפנייה")]
        base["letter"] = ""
    # תאימות לתצוגות שמציגות את הצעדים כשורות טקסט
    base["step_lines"] = [st["t"] + (f": {st['d']}" if st["d"] else "") for st in base["steps"]]
    return base
