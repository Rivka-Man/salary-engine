"""
תלוש שכר מול החישוב: מסך "התלוש שלי".

1. extract_payslip - Claude קורא את התלוש (PDF סרוק או צילום) ומחזיר את חודש
   התלוש ואת שורת גמול ההשתלמות של עוז לתמורה (סמל 249: מספר יחידות הגמול
   וסכום חודשי). התלוש של משרד החינוך הוא בדרך כלל תמונה סרוקה בלי טקסט,
   ולכן צריך קריאת תמונה ולא חילוץ טקסט רגיל. המורה מאשרת/מתקנת לפני שמירה.
2. compare - משווה את מספר היחידות בתלוש למספר היחידות שהמנוע חישב, מתרגם את
   הפער לאחוזים לפי שווי היחידה שבחוקה (pct_per_unit) ולש"ח לפי שווי היחידה
   בתלוש עצמו, מזהה סיבה אחת לפער ומנסח את הפעולה לתיקון (כולל נוסח פנייה).

בעמודת "אחוז/כמות" של שורת הגמול מופיע מספר יחידות (למשל 9.75), לא אחוז.
הגמול בש"ח = יחידות × שווי יחידה (1.3% מ-1.9.2025) × השכר בחלק המשרה שבעוז.
משווים יחידות ולא סכומים: הסכום תלוי בהיקף המשרה ובשכר, היחידות לא.

דורש ANTHROPIC_API_KEY לחילוץ אוטומטי. בלעדיו אפשר להזין את השורה ידנית.
"""
import base64
import itertools
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


def parse_month(raw):
    """'2026-09' / '09/2026' / '9.2026' / 'ספטמבר 2026' -> '2026-09'. None אם ריק או לא מזוהה."""
    s = re.sub(r"[^\w/.\- ]", "", raw or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", s) or None
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
    else:
        m = re.fullmatch(r"(\d{1,2})\s*[/.\-]\s*(\d{4})", s)
        if m:
            mo, y = int(m.group(1)), int(m.group(2))
        else:
            name = next((i + 1 for i, n in enumerate(MONTHS_HE) if n in s), None)
            year = re.search(r"\d{4}", s)
            if not (name and year):
                return None
            mo, y = name, int(year.group(0))
    return f"{y:04d}-{mo:02d}" if 1 <= mo <= 12 and 2000 <= y <= 2100 else None


def parse_number(raw, decimal_comma=False):
    """מספר מתוך מה שהוקלד או הודבק מהתלוש: מתעלם מתווים נסתרים (סימני כיוון),
    מ-%, ₪, ש"ח, יח' וכו'. decimal_comma: '9,75' הוא 9.75 (ליחידות). אחרת פסיק
    הוא מפריד אלפים ('1,277.20'). None אם ריק; ValueError אם אין בו מספר תקין."""
    s = re.sub(r"[^0-9.,\-]", "", raw or "")
    if not s:
        if re.search(r"\S", re.sub(r"[‎‏‪-‮⁦-⁩]", "", raw or "")):
            raise ValueError(raw)
        return None
    if "," in s and "." not in s and (decimal_comma or re.fullmatch(r"-?\d+,\d{1,2}", s)):
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    return float(s)


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
        # עמודות נוספות בתלוש (לתצוגת התלוש המלא): מ-/עד- (MM/YYYY), הפרשים,
        # ובמקטע הגמל: הפרשת מעסיק ופיצויים
        "from_month": _nullable({"type": "string"}),
        "to_month": _nullable({"type": "string"}),
        "diff": _nullable({"type": "number"}),
        "employer": _nullable({"type": "number"}),
        "severance": _nullable({"type": "number"}),
    },
    "required": ["code", "description", "qty_or_pct", "amount", "from_month", "to_month", "diff", "employer", "severance"],
    "additionalProperties": False,
}

_SECTION = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "kind": {"type": "string", "enum": ["payments", "mandatory", "gemel", "other"]},
        "lines": {"type": "array", "items": _LINE},
    },
    "required": ["title", "kind", "lines"],
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
                "units": _nullable({"type": "number"}),
                "amount": _nullable({"type": "number"}),
            },
            "required": ["code", "description", "units", "amount"],
            "additionalProperties": False,
        }),
        "oz_lines": {"type": "array", "items": _LINE},
        # התלוש המלא, לתצוגת "התלוש החי" במסך התוצאה: כל המקטעים כפי שהם בתלוש
        "sections": {"type": "array", "items": _SECTION},
        "totals": {
            "type": "object",
            "properties": {
                "gross": _nullable({"type": "number"}),
                "mandatory_deductions": _nullable({"type": "number"}),
                "gemel_deductions": _nullable({"type": "number"}),
                "net": _nullable({"type": "number"}),
            },
            "required": ["gross", "mandatory_deductions", "gemel_deductions", "net"],
            "additionalProperties": False,
        },
        "notes": {"type": "string"},
    },
    "required": ["is_payslip", "pay_month", "gimul", "oz_lines", "sections", "totals", "notes"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """את/ה קורא/ת תלוש שכר של עובד/ת הוראה ממשרד החינוך (לרוב סרוק כתמונה).

המטרה: לחלץ את שורת גמול ההשתלמות של עוז לתמורה.
- בטבלת "תשלומים" יש בלוק שורות של עוז לתמורה שמסתיים בשורה "סה"כ עוז לתמורה".
- שורת הגמול היא בדרך כלל סמל 249 עם התיאור "ג. השת. עוז לתמורה" (או ניסוח דומה של גמול השתלמות בעוז לתמורה).
- בשורה הזו, בעמודה "אחוז/כמות" מופיע מספר יחידות הגמול (למשל 9.75) - זה מספר יחידות, לא אחוז. בעמודה "סכום חודשי" מופיע הסכום בש"ח.
- אל תבלבלו עם שורות אחרות בבלוק: "תואר עוז לתמורה", "הכשרה עוז לתמורה", "תוספת עוז לתמורה", "שכר משולב עוז", "שקלית". גם לא עם גמול השתלמות של אופק חדש.
- pay_month: חודש המשכורת מהכותרת "משכורת לחודש ..." בפורמט YYYY-MM.
- gimul: השורה שנמצאה. units כמספר (9.75). אם אין שורת גמול השתלמות של עוז לתמורה - null.
- oz_lines: כל השורות בבלוק עוז לתמורה כפי שהן (סמל, תיאור, אחוז/כמות כטקסט כפי שמופיע, סכום), לא כולל שורת הסה"כ.
- is_payslip: false אם המסמך אינו תלוש שכר.
- sections: התלוש המלא, מקטע אחרי מקטע לפי הסדר בתלוש: "תשלומים" (kind=payments, כל השורות כולל בלוק עוז לתמורה ושורת הסה"כ שלו), "ניכויי חובה" (kind=mandatory), "ניכויים/הפרשות גמל" (kind=gemel: description=שם הקופה, qty_or_pct=הבסיס לגמל כטקסט, employer=הפרשת מעסיק, severance=פיצויים, amount=ניכוי העובד), ושאר המקטעים (kind=other). בכל שורה: סמל (אם יש), תיאור, אחוז/כמות כטקסט, סכום, ומה שמופיע בעמודות מ-/עד- (MM/YYYY) והפרשים. שדה שאין לו ערך בשורה - null.
- totals: סה"כ תשלומים (gross), סה"כ ניכויי חובה, סה"כ ניכויי גמל, נטו לתשלום (net). null למה שלא מופיע.
- notes: הערה קצרה בעברית רק אם משהו לא ודאי (ספרה לא קריאה וכו'), אחרת מחרוזת ריקה.
אל תכללו פרטים אישיים (שם, ת"ז, כתובת, חשבון בנק, מספר עובד) בשום שדה, גם לא במקטעים."""


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
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": [block, {"type": "text", "text": "חלצו את שורת גמול ההשתלמות של עוז לתמורה, ואת התלוש המלא לפי מקטעים."}]}],
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


def _fmt(x, digits=3):
    if x is None:
        return "—"
    s = f"{x:,.{digits}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _money(x):
    return "—" if x is None else f"{x:,.2f}"


def _units(x):
    """'יחידה אחת' / '2 יחידות' / '9.75 יחידות'."""
    return "יחידה אחת" if abs((x or 0) - 1) < 1e-9 else f"{_fmt(x)} יחידות"


def _salary_lines(slip, pct):
    """השורות בבלוק עוז לתמורה שסכומן הוא השכר שעליו מחושב הגמול (לפי מה ש-Claude
    חילץ), כדי להראות למורה ממה בדיוק נגזר הסכום. None אם לא נמצא צירוף מדויק."""
    try:
        lines = json.loads(slip["extraction_json"] or "{}").get("oz_lines") or []
    except (ValueError, AttributeError):
        return None
    amount = slip["gimul_amount"]
    if not amount or not pct:
        return None
    cands = [l for l in lines if l.get("amount") and l["amount"] > 0 and (l.get("code") or "") != GIMUL_CODE][:8]
    for k in (1, 2, 3):
        for combo in itertools.combinations(cands, k):
            base = sum(l["amount"] for l in combo)
            if abs(base * pct / 100 - amount) < 0.011:
                return list(combo)
    return None


def _oz_base(slip):
    """השכר שעליו מחושב הגמול, כשאין שורת גמול להסיק ממנה: סכום שורות השכר בבלוק
    עוז לתמורה (בלי שורות באחוזים, בלי הגמול ובלי שורת הסה"כ). None אם אין נתונים."""
    try:
        lines = json.loads(slip["extraction_json"] or "{}").get("oz_lines") or []
    except (ValueError, AttributeError, TypeError):
        return None
    total = 0.0
    for l in lines:
        q, d = (l.get("qty_or_pct") or ""), (l.get("description") or "")
        if not l.get("amount") or "%" in q or (l.get("code") or "") == GIMUL_CODE or "סה" in d:
            continue
        if "השת" in d:
            continue
        total += float(l["amount"])
    return total or None


def compare(conn, result, slip):
    """
    result - פלט compute_gimul_hishtalmut לחודש התלוש. slip - שורה מטבלת payslips.
    מחזיר dict לתצוגה: מה בתלוש, מה בחישוב, הפער, הסיבה, והפעולה.
    """
    refs = _rule_refs(conn)
    rate = result.get("pct_per_unit") or 1.3  # שווי יחידה לפי החוקה, באחוזים מהשכר
    expected_units = float(result["U_total"] or 0)
    expected_pct = round(expected_units * rate, 4)

    line_missing = slip["gimul_units"] is None
    paid_units = float(slip["gimul_units"] or 0)
    paid_pct = round(paid_units * rate, 4)
    amount = slip["gimul_amount"]
    # שווי יחידה בש"ח בתלוש הזה, והשכר שעליו מחושב הגמול (חלק המשרה בעוז)
    unit_ils = (float(amount) / paid_units) if amount and paid_units else None
    base = (float(amount) / (paid_pct / 100)) if amount and paid_pct else None

    gap_units = round(expected_units - paid_units, 3)
    gap_pct = round(gap_units * rate, 4)
    if unit_ils is None:
        # אין שורת גמול בתלוש (או 0 יחידות): הערכה לפי השכר בבלוק עוז לתמורה
        oz_base = _oz_base(slip)
        unit_ils = oz_base * rate / 100 if oz_base else None
    gap_ils = round(unit_ils * gap_units, 2) if unit_ils is not None else None

    # פירוק החישוב לרכיבים, כדי להראות מאיפה מגיעה כל יחידה
    components = []
    if result.get("U_base_from_transition"):
        base_units = float(result["U_base_from_transition"])
        # R08: יחידת מעבר (יחידה 17) - מוצגת כרכיב נפרד, כי היא נתון שהמשרד מכיר
        # ובכל זאת לעיתים קרובות לא מגיעה לתלוש
        transition = any(t.get("rule_id") == "R08_TRANSITION_UNIT" and t.get("value")
                         for t in result.get("trace") or [])
        if transition and base_units >= 1:
            components.append({"key": "base", "label": "יחידות שהוכרו ערב ההצטרפות לעוז לתמורה",
                               "units": base_units - 1})
            components.append({"key": "transition", "label": "יחידת מעבר (יחידה 17)",
                               "units": 1.0, "rule_id": "R08_TRANSITION_UNIT"})
        else:
            components.append({"key": "base", "label": "יחידות שהוכרו ערב ההצטרפות לעוז לתמורה",
                               "units": base_units})
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
        components.append({"key": "dd", "label": "גמול כפל תואר / ביניים", "units": float(result["U_dd_or_mid"]),
                           "rule_id": "R04_DOUBLE_DEGREE"})

    cause = _diagnose(result, refs, rate, expected_units, paid_units, gap_units, components, line_missing)

    flagged = set(cause.get("flag_keys", []))
    for c in components:
        c["flagged"] = c["key"] in flagged

    explained = []
    if line_missing:
        explained.append("בתלוש אין שורת גמול השתלמות של עוז לתמורה (סמל 249) — כלומר לא שולם גמול השתלמות על חלק המשרה בעוז לתמורה.")
    else:
        explained.append(
            f"שורה {slip['gimul_code'] or GIMUL_CODE} \"{slip['gimul_desc'] or 'ג. השת. עוז לתמורה'}\": "
            f"{_units(paid_units)} גמול" + (f", {_money(amount)} ₪ בחודש." if amount else "."))
        explained.append(
            f"שווי יחידה לפי החוקה: {_fmt(rate)}% מהשכר. {_fmt(paid_units)} יח' × {_fmt(rate)}% = "
            f"{_fmt(paid_pct)}% תוספת לשכר.")
        if base:
            lines = _salary_lines(slip, paid_pct)
            if lines:
                parts = " + ".join(f"{_money(l['amount'])} {l['description']}" for l in lines)
                lines_total = sum(l["amount"] for l in lines)
                explained.append(
                    f"הסכום: {_fmt(paid_pct)}% × ({parts}) = {_fmt(paid_pct)}% × {_money(lines_total)} ₪ = {_money(amount)} ₪.")
            else:
                explained.append(
                    f"הסכום: {_fmt(paid_pct)}% מהשכר בחלק המשרה שבעוז לתמורה (כ-{_fmt(base, 0)} ₪) = {_money(amount)} ₪.")
            explained.append(f"כלומר כל יחידת גמול שווה לך כ-{_money(unit_ils)} ₪ בחודש. משווים יחידות, לא סכומים.")

    return {
        "rate": rate, "unit_ils": unit_ils,
        "paid_pct": paid_pct, "paid_units": paid_units, "amount": amount, "base": base,
        "line_missing": line_missing,
        "expected_pct": expected_pct, "expected_units": expected_units,
        "gap_units": gap_units, "gap_pct": gap_pct, "gap_ils": gap_ils,
        "components": components,
        "explained": explained,
        "cause": cause,
        "ok": cause["kind"] == "ok",
        "contact": CONTACT,
    }


def _diagnose(result, refs, rate, expected_units, paid_units, gap_units, components, line_missing):
    """הסיבה האחת לפער, לפי הסדר: אין פער / יותר בתלוש / כפל תואר / שנים שלא עודכנו / כללי."""
    tol = 0.01
    if abs(gap_units) < tol:
        return {"kind": "ok", "title": "התלוש תואם לחישוב",
                "text": f"בתלוש מוכרות {_units(paid_units)}, בדיוק כמו בחישוב. אין צורך לעשות דבר."}

    if gap_units < 0:
        return {
            "kind": "overpaid", "title": "בתלוש מוכרות יותר יחידות ממה שחישבנו",
            "text": (f"בתלוש מוכרות {_units(paid_units)}, ובחישוב שלנו רק {_fmt(expected_units)}. "
                     "בדרך כלל זה אומר שחסרים במערכת נתונים: קורסים שלא נטענו, או יחידות שהוכרו לפני ההצטרפות לעוז לתמורה."),
            "action": "לפני שפונים לחשבות: כדאי לוודא שכל הקורסים שלך מ\"הקורסים שלי\" בפורטל נטענו, ושיחידות ערב הרפורמה הוזנו, ואז לחשב שוב.",
            "fix_data": True,
        }

    dd = float(result.get("U_dd_or_mid") or 0)
    if dd and abs(gap_units - dd) < tol:
        return {
            "kind": "dd", "title": "גמול כפל תואר / ביניים לא נכלל בתלוש",
            "text": f"הפער הוא בדיוק {_units(dd)} — היחידות של גמול כפל תואר / ביניים, שלא נכללו בגמול בתלוש.",
            "action": "לבקש מחשבות השכר להוסיף את גמול כפל התואר / הביניים ולשלם הפרשים.",
            "attach": ["צילום התלוש", "תעודות התואר (שני התארים) או אישור גמול ביניים"],
            "rule_ref": _ref_text(refs, "R04_DOUBLE_DEGREE"),
            "letter_reason": f"לא נכללו בגמול {_units(dd)} של גמול כפל תואר / ביניים, שאני זכאית להן.",
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
                              f"לפי ההשתלמויות שלי אני זכאית ל-{_units(expected_units)}, "
                              f"ובתלוש מוכרות {_units(paid_units)}."),
            "flag_keys": [c["key"] for c in picked],
        }

    # צירוף של רכיבים שסכומו בדיוק הפער (למשל יחידת מעבר + כפל תואר)
    parts = [c for c in components if c["units"] > 0 and c["key"] != "base"]
    for k in (2, 3):
        hit = next((combo for combo in itertools.combinations(parts, k)
                    if abs(sum(c["units"] for c in combo) - gap_units) < tol), None)
        if hit:
            names = " ו".join(f"{c['label']} ({_fmt(c['units'])})" for c in hit)
            calc = " + ".join(_fmt(c["units"]) for c in hit)
            return {
                "kind": "parts", "title": f"חסרות בתלוש {_units(gap_units)}",
                "text": (f"לפי החישוב מגיעות לך {_units(expected_units)}, ובתלוש מוכרות {_fmt(paid_units)}. "
                         f"הפער הוא בדיוק {calc} = {_fmt(gap_units)}: {names}."),
                "action": "לבקש מחשבות השכר לעדכן את הגמול לפי הרכיבים החסרים, ולשלם הפרשים מהמועד שבו נוצרה הזכאות.",
                "attach": ["צילום התלוש", "אישור \"הקורסים שלי\" מהפורטל"]
                          + (["תעודות התואר (שני התארים)"] if any(c["key"] == "dd" for c in hit) else []),
                "rule_ref": _ref_text(refs, hit[0].get("rule_id") or "R11_ANNUAL_CAP"),
                "letter_reason": (f"לא נכללו בגמול {_units(gap_units)}: {names}. "
                                  f"לפי הנתונים שלי אני זכאית ל-{_units(expected_units)}, "
                                  f"ובתלוש מוכרות {_units(paid_units)}."),
                "flag_keys": [c["key"] for c in hit],
            }

    if line_missing:
        title = "גמול ההשתלמות לא מופיע בתלוש"
        text = f"לפי החישוב מגיעות לך {_units(expected_units)}, ובתלוש אין שורת גמול השתלמות של עוז לתמורה בכלל."
    else:
        title = f"חסרות בתלוש {_units(gap_units)}"
        text = f"לפי החישוב מגיעות לך {_units(expected_units)}, ובתלוש מוכרות {_fmt(paid_units)}."
    return {
        "kind": "units", "title": title, "text": text,
        "action": "לבקש מחשבות השכר לבדוק מחדש את גמול ההשתלמות לפי רשימת ההשתלמויות המלאה, ולשלם הפרשים.",
        "attach": ["צילום התלוש", "אישור \"הקורסים שלי\" מהפורטל"],
        "rule_ref": _ref_text(refs, "R11_ANNUAL_CAP"),
        "letter_reason": (f"לפי ההשתלמויות שלי אני זכאית ל-{_units(expected_units)}, "
                          f"ובתלוש מוכרות {_units(paid_units)}."),
        # כשהשורה חסרה בתלוש, כל הרכיבים חסרים בו
        "flag_keys": [c["key"] for c in components if c["units"] > 0] if line_missing else [],
    }


def letter(cmp, slip, teacher_name):
    """נוסח פנייה מוכן להעתקה לפניות המקוונות בפורטל."""
    cause = cmp["cause"]
    if not cause.get("letter_reason"):
        return ""
    if cmp["line_missing"]:
        opening = f"בתלוש לחודש {month_label(slip['pay_month'])} לא מופיע גמול השתלמות עוז לתמורה."
    else:
        opening = (f"בתלוש לחודש {month_label(slip['pay_month'])} שולם לי גמול השתלמות עוז לתמורה "
                   f"(סמל {slip['gimul_code'] or GIMUL_CODE}) עבור {_units(cmp['paid_units'])}.")
    lines = ["שלום רב,", "", opening, cause["letter_reason"]]
    if cause.get("rule_ref"):
        lines.append(f"הבסיס: {cause['rule_ref']}.")
    lines += ["", "אבקש " + cause["action"].removeprefix("לבקש מחשבות השכר ")]
    if cause.get("attach"):
        lines += ["", "מצורפים: " + ", ".join(cause["attach"]) + "."]
    lines += ["", "תודה,", teacher_name or ""]
    return "\n".join(lines).strip()


# ------------------------------------------------------------------
# "התלוש החי": התלוש כפי שחולץ, לתצוגה בסגנון תלוש משרד החינוך
# ------------------------------------------------------------------

def _is_gimul(line):
    code = (line.get("code") or "").strip()
    desc = line.get("description") or ""
    return code == GIMUL_CODE or ("השת" in desc and "עוז" in desc and "סה" not in desc)


def slip_view(slip):
    """
    המקטעים להצגת התלוש. עדיפות: התלוש המלא שחולץ (sections) -> בלוק עוז לתמורה
    (oz_lines) -> שורת הגמול בלבד. בכל מקרה שורת הגמול מסומנת is_gimul, וכשהיא
    חסרה בתלוש נוספת שורה ריקה במקומה (שם יוקף העיגול הוורוד).
    מחזיר {"sections": [...], "totals": {...} | None, "partial": bool}.
    """
    try:
        ext = json.loads(slip["extraction_json"] or "{}") or {}
    except (ValueError, TypeError):
        ext = {}
    gimul_line = {"code": slip["gimul_code"] or GIMUL_CODE,
                  "description": slip["gimul_desc"] or "ג. השת. עוז לתמורה",
                  "qty_or_pct": None if slip["gimul_units"] is None else _fmt(slip["gimul_units"], 2),
                  "amount": slip["gimul_amount"]}
    sections = [dict(sec, lines=[dict(l) for l in sec.get("lines") or []]) for sec in ext.get("sections") or []]
    partial = False
    if not sections:
        partial = True
        lines = [dict(l) for l in ext.get("oz_lines") or []]
        sections = [{"title": "תשלומים", "kind": "payments", "lines": lines}]
    pay = next((sec for sec in sections if sec.get("kind") == "payments"), sections[0])
    found = False
    for sec in sections:
        for l in sec["lines"]:
            if _is_gimul(l):
                l.update(is_gimul=True)
                # מה שאושר בטופס גובר על מה שחולץ
                l["qty_or_pct"], l["amount"] = gimul_line["qty_or_pct"], gimul_line["amount"]
                found = True
    if not found:
        # השורה לא בתלוש (או שלא נקראה): שורה ריקה בסוף בלוק עוז לתמורה
        idx = next((i for i, l in enumerate(pay["lines"]) if "סה" in (l.get("description") or "")
                    and "עוז" in (l.get("description") or "")), len(pay["lines"]))
        pay["lines"].insert(idx, dict(gimul_line, is_gimul=True, missing=slip["gimul_units"] is None))
    for sec in sections:
        for l in sec["lines"]:
            l["is_total"] = "סה\"כ" in (l.get("description") or "") or "סה''כ" in (l.get("description") or "")
    return {"sections": sections, "totals": ext.get("totals"), "partial": partial}
