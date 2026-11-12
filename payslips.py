"""
תלוש שכר מול החישוב: מסך "התלוש שלי".

1. extract_payslip - Claude קורא את התלוש (PDF סרוק או צילום) ומחזיר את חודש
   התלוש ואת שורת גמול ההשתלמות של עוז לתמורה (סמל 249: אחוז וסכום חודשי).
   התלוש של משרד החינוך הוא בדרך כלל תמונה סרוקה בלי טקסט, ולכן צריך קריאת
   תמונה ולא חילוץ טקסט רגיל. המורה מאשרת/מתקנת את הערכים לפני שמירה.
2. compare - משווה את האחוז בתלוש לאחוז שהמנוע חישב, מתרגם לפער ביחידות
   ובש"ח, מזהה סיבה אחת לפער ומנסח את הפעולה לתיקון (כולל נוסח פנייה).

משווים אחוזים ולא סכומים: הסכום בתלוש מחושב רק על חלק המשרה שבעוז לתמורה,
ואילו האחוז הוא מה שהגמול קובע.

דורש ANTHROPIC_API_KEY לחילוץ אוטומטי. בלעדיו אפשר להזין את השורה ידנית.
"""
import base64
import json
import os
import re

MODEL = "claude-opus-5-5"
MAX_FILE_BYTES = 15 * 1024 * 1024
GIMUL_CODE = "249"

# פנייה לחשבות השכר - כפי שמופיע בהודעות לעובד/ת בתלוש עצמו
CONTACT = {
    "portal": "https://pob.education.gov.il",
    "portal_label": "פורטל עובדי הוראה ← פניות מקוונות",
    "phone": "‎*6552",
}

MONTHS_HE = ["ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני", "יולי",
             "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר"]


class PayslipError(Exception):
    """שגיאה עם הודעה בעברית שמוצגת למשתמשת."""


def month_label(pay_month):
    """'2026-09' -> 'ספטמבר 2026'."""
    try:
        y, m = pay_month.split("-")
        return f"{MONTHS_HE[int(m) - 1]} {y}"
    except (ValueError, IndexError, AttributeError):
        return pay_month or ""


def valid_month(value):
    return bool(value) and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value) is not None


# ------------------------------------------------------------------
# חילוץ עם Claude
# ------------------------------------------------------------------

def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


_LINE = {
    "type": "object",
    "properties": {
        "code": _nullable({"type": "string"}),
        "description": {"type": "string"},
        "qty_or_pct": _nullable({"type": "string"}),
        "amount": _nullable({"type": "number"}),
    },
    "required": ["code", "description", "qty_or_pct", "amount"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_payslip": {"type": "boolean"},
        "pay_month": _nullable({"type": "string"}),
        "gimul": _nullable({
            "type": "object",
            "properties": {
                "code": _nullable({"type": "string"}),
                "description": {"type": "string"},
                "pct": _nullable({"type": "number"}),
                "amount": _nullable({"type": "number"}),
            },
            "required": ["code", "description", "pct", "amount"],
            "additionalProperties": False,
        }),
        "oz_lines": {"type": "array", "items": _LINE},
        "notes": {"type": "string"},
    },
    "required": ["is_payslip", "pay_month", "gimul", "oz_lines", "notes"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """את/ה קורא/ת תלוש שכר של עובד/ת הוראה ממשרד החינוך (לרוב סרוק כתמונה).

המטרה: לחלץ את שורת גמול ההשתלמות של עוז לתמורה.
- בטבלת "תשלומים" יש בלוק שורות של עוז לתמורה שמסתיים בשורה "סה"כ עוז לתמורה".
- שורת הגמול היא בדרך כלל סמל 249 עם התיאור "ג. השת. עוז לתמורה" (או ניסוח דומה של גמול השתלמות בעוז לתמורה).
- בעמודה "אחוז/כמות" מופיע אחוז הגמול (למשל 9.75, לפעמים בלי סימן %). בעמודה "סכום חודשי" מופיע הסכום בש"ח.
- אל תבלבלו עם שורות אחרות בבלוק: "תואר עוז לתמורה", "הכשרה עוז לתמורה", "תוספת עוז לתמורה", "שכר משולב עוז", "שקלית". גם לא עם גמול השתלמות של אופק חדש.
- pay_month: חודש המשכורת מהכותרת "משכורת לחודש ..." בפורמט YYYY-MM.
- gimul: השורה שנמצאה. pct כמספר (9.75, לא "9.75%"). אם אין שורת גמול השתלמות של עוז לתמורה - null.
- oz_lines: כל השורות בבלוק עוז לתמורה כפי שהן (סמל, תיאור, אחוז/כמות כטקסט, סכום), לא כולל שורת הסה"כ.
- is_payslip: false אם המסמך אינו תלוש שכר.
- notes: הערה קצרה בעברית רק אם משהו לא ודאי (ספרה לא קריאה וכו'), אחרת מחרוזת ריקה.
אל תכללו פרטים אישיים (שם, ת"ז, כתובת, חשבון בנק) בשום שדה."""


def file_block(filename, data):
    """בלוק התוכן של התלוש לבקשה ל-Claude: PDF או תמונה."""
    if len(data) > MAX_FILE_BYTES:
        raise PayslipError("הקובץ גדול מדי (עד 15MB).")
    name = (filename or "").lower()
    b64 = base64.standard_b64encode(data).decode("ascii")
    if name.endswith(".pdf") or data[:5] == b"%PDF-":
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    for ext, media in ((".png", "image/png"), (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"), (".webp", "image/webp")):
        if name.endswith(ext):
            return {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}}
    raise PayslipError("סוג קובץ לא נתמך. אפשר לטעון PDF או צילום (JPG / PNG).")


def extract_payslip(filename, data, client=None):
    """שולח את התלוש ל-Claude ומחזיר dict לפי OUTPUT_SCHEMA. client ניתן להחלפה לבדיקות."""
    block = file_block(filename, data)
    if client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise PayslipError("קריאה אוטומטית של התלוש לא זמינה כרגע (לא הוגדר ANTHROPIC_API_KEY). אפשר להזין את השורה ידנית.")
        import anthropic
        client = anthropic.Anthropic()
    import anthropic
    try:
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=8000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": [block, {"type": "text", "text": "חלצו את שורת גמול ההשתלמות של עוז לתמורה מהתלוש."}]}],
        ) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise PayslipError("מפתח ה-API של Anthropic שגוי. אפשר להזין את השורה ידנית.")
    except anthropic.RateLimitError:
        raise PayslipError("שירות הקריאה עמוס כרגע. נסי שוב בעוד דקה, או הזיני את השורה ידנית.")
    except anthropic.APIStatusError as exc:
        raise PayslipError(f"שגיאה בשירות הקריאה ({exc.status_code}). אפשר להזין את השורה ידנית.")
    except anthropic.APIConnectionError:
        raise PayslipError("אין חיבור לשירות הקריאה. אפשר להזין את השורה ידנית.")

    if message.stop_reason in ("refusal", "max_tokens"):
        raise PayslipError("לא הצלחתי לקרוא את התלוש. אפשר להזין את השורה ידנית.")
    text = next((b.text for b in message.content if b.type == "text"), "")
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        raise PayslipError("לא הצלחתי לקרוא את התלוש. אפשר להזין את השורה ידנית.")
    if not result.get("is_payslip"):
        raise PayslipError("הקובץ לא נראה כמו תלוש שכר.")
    if result.get("pay_month") and not valid_month(result["pay_month"]):
        result["pay_month"] = None
    return result


# ------------------------------------------------------------------
# השוואה: התלוש מוסבר, פער אחד, פעולה אחת
# ------------------------------------------------------------------

def _rule_refs(conn):
    rows = conn.execute("SELECT rule_id, title, source_doc, source_ref FROM rules").fetchall()
    return {r["rule_id"]: r for r in rows}


def _ref_text(refs, rule_id):
    r = refs.get(rule_id)
    if not r:
        return ""
    doc = (r["source_doc"] or "").replace("_", " ")
    return f"{doc}, {r['source_ref']}" if r["source_ref"] else doc


def _fmt(x, digits=2):
    if x is None:
        return "—"
    s = f"{x:,.{digits}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _units(x):
    """'יחידה אחת' / '2 יחידות' / '1.5 יחידות'."""
    return "יחידה אחת" if abs((x or 0) - 1) < 1e-9 else f"{_fmt(x)} יחידות"


def compare(conn, result, slip):
    """
    result - פלט compute_gimul_hishtalmut לחודש התלוש. slip - שורה מטבלת payslips.
    מחזיר dict לתצוגה: מה בתלוש, מה בחישוב, הפער, הסיבה, והפעולה.
    """
    refs = _rule_refs(conn)
    rate = result.get("pct_per_unit") or 1.3
    expected_units = float(result["U_total"] or 0)
    expected_pct = round(expected_units * rate, 4)

    paid_pct = slip["gimul_pct"]
    line_missing = paid_pct is None
    paid_pct = float(paid_pct or 0)
    paid_units = round(paid_pct / rate, 3) if rate else 0.0
    amount = slip["gimul_amount"]
    # השכר שעליו מחושב הגמול (חלק המשרה בעוז לתמורה), נגזר מהתלוש עצמו
    base = (float(amount) / (paid_pct / 100)) if amount and paid_pct else None

    gap_units = round(expected_units - paid_units, 3)
    gap_pct = round(expected_pct - paid_pct, 4)
    gap_ils = round(base * gap_pct / 100, 2) if base is not None else None

    # פירוק החישוב לרכיבים, כדי להראות מאיפה מגיעה כל יחידה
    components = []
    if result.get("U_base_from_transition"):
        components.append({"key": "base", "label": "יחידות שהוכרו ערב ההצטרפות לעוז לתמורה",
                           "units": float(result["U_base_from_transition"])})
    for y in result.get("year_breakdown") or []:
        if y.get("units_earned"):
            label = f"השתלמויות שנת {y['school_year']}"
            if y.get("status") and y["status"] != "רגיל":
                label += f" ({y['status']})"
            components.append({"key": f"year:{y['school_year']}", "label": label,
                               "units": float(y["units_earned"]), "school_year": y["school_year"]})
    if (result.get("U_regular_earned_before_cap") or 0) > (result.get("U_regular") or 0):
        components.append({"key": "cap", "label": f"חיתוך לתקרה ({_fmt(result['U_cap_applied'])} יח')",
                           "units": round(float(result["U_regular"]) - float(result["U_regular_earned_before_cap"]), 3)})
    if result.get("U_dd_or_mid"):
        components.append({"key": "dd", "label": "גמול כפל תואר / ביניים", "units": float(result["U_dd_or_mid"])})

    cause = _diagnose(result, refs, rate, expected_units, paid_pct, paid_units, gap_units, components, line_missing)

    flagged = set(cause.get("flag_keys", []))
    for c in components:
        c["flagged"] = c["key"] in flagged

    explained = []
    if line_missing:
        explained.append("בתלוש אין שורת גמול השתלמות של עוז לתמורה (סמל 249) — כלומר לא שולם גמול השתלמות על חלק המשרה בעוז לתמורה.")
    else:
        explained.append(
            f"שורה {slip['gimul_code'] or GIMUL_CODE} \"{slip['gimul_desc'] or 'ג. השת. עוז לתמורה'}\": "
            f"{_fmt(paid_pct)}% תוספת לשכר" + (f", {_fmt(amount)} ₪ בחודש." if amount else "."))
        explained.append(f"{_fmt(paid_pct)}% = {_fmt(paid_units)} יחידות גמול × {_fmt(rate)}% ליחידה.")
        if base:
            explained.append(
                f"הסכום מחושב רק על השכר בחלק המשרה שבעוז לתמורה: {_fmt(paid_pct)}% מ-{_fmt(base, 0)} ₪ = {_fmt(amount)} ₪. "
                "לכן משווים את האחוז, לא את הסכום.")

    return {
        "rate": rate,
        "paid_pct": paid_pct, "paid_units": paid_units, "amount": amount, "base": base,
        "line_missing": line_missing,
        "expected_pct": expected_pct, "expected_units": expected_units,
        "gap_units": gap_units, "gap_pct": gap_pct, "gap_ils": gap_ils,
        "components": components,
        "explained": explained,
        "cause": cause,
        "ok": cause["kind"] == "ok",
        "contact": CONTACT,
        "rate_ref": _ref_text(refs, "R07B_PCT_RATE_REFORM"),
    }


def _diagnose(result, refs, rate, expected_units, paid_pct, paid_units, gap_units, components, line_missing):
    """הסיבה האחת לפער, לפי הסדר: אין פער / שיעור ישן / כפל תואר / שנים שלא עודכנו / כללי."""
    tol = 0.01
    if abs(gap_units) < tol:
        return {"kind": "ok", "title": "התלוש תואם לחישוב",
                "text": f"שולמו {_fmt(paid_units)} יחידות, בדיוק כמו בחישוב. אין צורך לעשות דבר."}

    if gap_units < 0:
        return {
            "kind": "overpaid", "title": "בתלוש מוכרות יותר יחידות ממה שחישבנו",
            "text": (f"בתלוש מוכרות {_fmt(paid_units)} יחידות, ובחישוב שלנו רק {_fmt(expected_units)}. "
                     "בדרך כלל זה אומר שחסרים במערכת נתונים: קורסים שלא נטענו, או יחידות שהוכרו לפני ההצטרפות לעוז לתמורה."),
            "action": "לפני שפונים לחשבות: כדאי לוודא שכל הקורסים שלך מ\"הקורסים שלי\" בפורטל נטענו, ושיחידות ערב הרפורמה הוזנו, ואז לחשב שוב.",
            "fix_data": True,
        }

    # שולם בשיעור הישן (1.2%) במקום 1.3% - האחוז בתלוש מתאים בדיוק ליחידות שלנו בשיעור הישן
    if rate > 1.2 and expected_units and abs(paid_pct - expected_units * 1.2) < tol:
        return {
            "kind": "rate", "title": "הגמול שולם בשיעור הישן",
            "text": (f"ספרו לך {_fmt(expected_units)} יחידות — נכון — אבל כל יחידה שולמה ב-1.2% במקום {_fmt(rate)}%. "
                     "מאז 1.9.2025 שיעור הגמול לכל יחידה בעוז לתמורה הוא 1.3%."),
            "action": "לבקש מחשבות השכר לעדכן את שיעור הגמול ל-1.3% ליחידה ולשלם הפרשים.",
            "attach": ["צילום התלוש"],
            "rule_ref": _ref_text(refs, "R07B_PCT_RATE_REFORM"),
            "letter_reason": (f"בתלוש מוכרות {_fmt(expected_units)} יחידות, אך הן שולמו בשיעור 1.2% ליחידה. "
                              f"מאז 1.9.2025 השיעור בעוז לתמורה הוא 1.3% ליחידה, כך שהגמול הנכון הוא "
                              f"{_fmt(expected_units * rate)}% ולא {_fmt(paid_pct)}%."),
            "flag_keys": [],
        }

    dd = float(result.get("U_dd_or_mid") or 0)
    if dd and abs(gap_units - dd) < tol:
        return {
            "kind": "dd", "title": "גמול כפל תואר / ביניים לא נכלל בתלוש",
            "text": f"הפער הוא בדיוק {_fmt(dd)} יחידות — היחידות של גמול כפל תואר / ביניים, שלא נכללו בגמול בתלוש.",
            "action": "לבקש מחשבות השכר להוסיף את גמול כפל התואר / הביניים ולשלם הפרשים.",
            "attach": ["צילום התלוש", "תעודות התואר (שני התארים) או אישור גמול ביניים"],
            "rule_ref": _ref_text(refs, "R04_DOUBLE_DEGREE"),
            "letter_reason": f"לא נכללו בגמול {_fmt(dd)} יחידות של גמול כפל תואר / ביניים, שאני זכאית להן.",
            "flag_keys": ["dd"],
        }

    # שנים אחרונות שהיחידות שלהן לא הגיעו לתלוש: מהשנה האחרונה אחורה עד שמכסים את הפער
    years = [c for c in components if c["key"].startswith("year:")]
    picked, total = [], 0.0
    for c in reversed(years):
        if total >= gap_units - tol:
            break
        picked.append(c)
        total += c["units"]
    if picked and abs(total - gap_units) < tol:
        picked.reverse()
        names = " ו-".join(c["school_year"] for c in picked)
        units_txt = _units(gap_units)
        return {
            "kind": "years", "title": f"ההשתלמויות של {names} עוד לא עודכנו בתלוש",
            "text": (f"לפי החישוב צברת {_fmt(expected_units)} יחידות, ובתלוש מוכרות {_fmt(paid_units)}. "
                     + ("היחידה החסרה היא בדיוק היחידה שנצברה" if abs(gap_units - 1) < tol
                        else f"ה-{units_txt} החסרות הן בדיוק היחידות שנצברו")
                     + f" מההשתלמויות של {names}."),
            "action": f"לבקש מחשבות השכר לעדכן את הגמול לפי ההשתלמויות של {names}, ולשלם הפרשים מהמועד שבו נוצרה הזכאות.",
            "attach": ["צילום התלוש", f"אישור \"הקורסים שלי\" מהפורטל, עם הקורסים של {names}"],
            "rule_ref": _ref_text(refs, "R11_ANNUAL_CAP"),
            "letter_reason": (f"לא עודכנו בגמול {units_txt} שצברתי מהשתלמויות {names}. "
                              f"לפי ההשתלמויות שלי אני זכאית ל-{_units(expected_units)} "
                              f"({_fmt(expected_units * rate)}%), ובתלוש שולמו {_units(paid_units)} ({_fmt(paid_pct)}%)."),
            "flag_keys": [c["key"] for c in picked],
        }

    if line_missing:
        title = "גמול ההשתלמות לא מופיע בתלוש"
        text = f"לפי החישוב מגיעות לך {_fmt(expected_units)} יחידות ({_fmt(expected_units * rate)}%), ובתלוש אין שורת גמול השתלמות של עוז לתמורה בכלל."
    else:
        title = f"חסרות בתלוש {_units(gap_units)}"
        text = f"לפי החישוב מגיעות לך {_fmt(expected_units)} יחידות, ובתלוש מוכרות {_fmt(paid_units)}."
    return {
        "kind": "units", "title": title, "text": text,
        "action": "לבקש מחשבות השכר לבדוק מחדש את גמול ההשתלמות לפי רשימת ההשתלמויות המלאה, ולשלם הפרשים.",
        "attach": ["צילום התלוש", "אישור \"הקורסים שלי\" מהפורטל"],
        "rule_ref": _ref_text(refs, "R11_ANNUAL_CAP"),
        "letter_reason": (f"לפי ההשתלמויות שלי אני זכאית ל-{_fmt(expected_units)} יחידות ({_fmt(expected_units * rate)}%), "
                          f"ובתלוש שולמו {_fmt(paid_units)} יחידות ({_fmt(paid_pct)}%)."),
        "flag_keys": [],
    }


def letter(cmp, slip, teacher_name):
    """נוסח פנייה מוכן להעתקה לפניות המקוונות בפורטל."""
    cause = cmp["cause"]
    if not cause.get("letter_reason"):
        return ""
    lines = [
        "שלום רב,",
        "",
        f"בתלוש לחודש {month_label(slip['pay_month'])} שולם לי גמול השתלמות עוז לתמורה (סמל {slip['gimul_code'] or GIMUL_CODE}) "
        f"בשיעור {_fmt(cmp['paid_pct'])}%.",
        cause["letter_reason"],
    ]
    if cause.get("rule_ref"):
        lines.append(f"הבסיס: {cause['rule_ref']}.")
    lines += [
        "",
        "אבקש " + cause["action"].removeprefix("לבקש מחשבות השכר "),
    ]
    if cause.get("attach"):
        lines += ["", "מצורפים: " + ", ".join(cause["attach"]) + "."]
    lines += ["", "תודה,", teacher_name or ""]
    return "\n".join(lines).strip()
