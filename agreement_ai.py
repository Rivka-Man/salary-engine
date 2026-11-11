"""
קריאת הסכם (PDF / Word / טקסט) עם Claude והפקת הצעת שינויים בחוקות.

Claude מקבל את ההסכם, את כל החוקות הקיימות (חוקים, תנאים, פרמטרים ותקופות
התוקף שלהם) ואת אוצר המילים של החוקה הטבלאית (rule_vocab), ומחזיר JSON במבנה
קבוע (structured outputs): תקציר, רשימת שינויים מוצעים, וסעיפים רלוונטיים
שלא ניתן לייצג בחוקה. הקוד כאן רק מפיק את ההצעה - הבדיקה מול ה-DB, התצוגה
וההחלה נעשות ב-agreements.py, ושום דבר לא נכתב לחוקה בלי אישור ידני.

דורש ANTHROPIC_API_KEY בסביבה (Render → Environment).
"""
import base64
import io
import json
import os
import re
import zipfile

from rule_vocab import ACTIONS, CHOICES, CONNECTORS, FIELDS, OPERATORS

MODEL = "claude-opus-5-5"
MAX_FILE_BYTES = 25 * 1024 * 1024

CHANGE_KINDS = {
    "param_period": "ערך חדש לפרמטר",
    "rule_update": "עדכון פרטי חוק",
    "rule_logic": "שינוי תנאים / פעולה",
    "rule_new": "חוק חדש",
}


class AgreementError(Exception):
    """שגיאה עם הודעה בעברית שמוצגת למשתמשת."""


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


_STR = {"type": "string"}
_NSTR = _nullable(_STR)

_CONDITION_SCHEMA = {
    "type": "object",
    "properties": {
        "connector": {"type": "string", "enum": list(CONNECTORS)},
        "field": {"type": "string", "enum": list(FIELDS)},
        "operator": {"type": "string", "enum": list(OPERATORS)},
        "value": _NSTR,
        "value_field": _NSTR,
    },
    "required": ["connector", "field", "operator", "value", "value_field"],
    "additionalProperties": False,
}

_FIELDS_SCHEMA = {
    "type": "object",
    "properties": {k: _NSTR for k in
                   ("title", "description", "source_doc", "source_ref", "applies_when", "valid_from", "valid_to")},
    "required": ["title", "description", "source_doc", "source_ref", "applies_when", "valid_from", "valid_to"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "agreement_title": {"type": "string"},
        "summary": {"type": "string"},
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(CHANGE_KINDS)},
                    "rule_id": {"type": "string"},
                    "constitution_id": _nullable({"type": "integer"}),
                    "param_name": _NSTR,
                    "param_value": _nullable({"type": "number"}),
                    "valid_from": _NSTR,
                    "valid_to": _NSTR,
                    "fields": _nullable(_FIELDS_SCHEMA),
                    "conditions": _nullable({"type": "array", "items": _CONDITION_SCHEMA}),
                    "action": _NSTR,
                    "result_value": _NSTR,
                    "priority": _nullable({"type": "integer"}),
                    "explanation": {"type": "string"},
                    "section": _NSTR,
                    "quote": {"type": "string"},
                },
                "required": ["kind", "rule_id", "constitution_id", "param_name", "param_value", "valid_from",
                             "valid_to", "fields", "conditions", "action", "result_value", "priority",
                             "explanation", "section", "quote"],
                "additionalProperties": False,
            },
        },
        "not_mapped": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"section": _NSTR, "quote": {"type": "string"}, "explanation": {"type": "string"}},
                "required": ["section", "quote", "explanation"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["agreement_title", "summary", "changes", "not_mapped"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """את/ה מנתח/ת הסכמי עבודה ושכר במערכת חינוך בישראל, עבור מערכת שמחשבת גמולים לעובדי הוראה.
המערכת מחזיקה "חוקות": כל חוקה היא טבלת חוקים. לכל חוק יש תנאים (על מי הוא חל), פעולה, תוצאה, ופרמטרים מספריים עם תקופות תוקף.
המנוע מחשב לפי ערכי הפרמטרים ותקופות התוקף שלהם; התנאים, הפעולה ופרטי החוק הם תיעוד מובנה.

המשימה: לקרוא את ההסכם המצורף ולהציע את השינויים בחוקות הקיימות שההסכם מחייב. ההצעה תוצג למנהלת, שתאשר או תדחה כל שינוי בנפרד.

סוגי שינויים (kind):
- param_period: ערך חדש לפרמטר קיים מתאריך מסוים (למשל אחוז ליחידה שעולה מ-1.2 ל-1.4 החל מ-01/09/2026). מלא/י rule_id, param_name (רק שם פרמטר שכבר קיים לחוק), param_value, valid_from (חובה), valid_to (null = ללא סיום). תקופה פתוחה חדשה סוגרת אוטומטית את התקופה הפתוחה הקודמת.
- rule_update: שינוי בפרטי חוק קיים. ב-fields מלא/י רק את השדות שמשתנים, וכל השאר null. כך גם מסיימים תוקף של חוק (valid_to).
- rule_logic: שינוי "חל על" / פעולה / תוצאה של חוק קיים. conditions היא הרשימה המלאה החדשה של התנאים (לא רק התוספת), action הוא קוד הפעולה המלא, result_value תוצאה מילולית או null.
- rule_new: חוק חדש שאין לו מקבילה. rule_id באנגלית (למשל R30_NEW_RULE, ייחודי), constitution_id של החוקה המתאימה, priority, fields (title, description, source_doc, source_ref לפחות), conditions, action, result_value. שימי/שים לב: חוק חדש מתועד בחוקה אבל המנוע לא יחשב לפיו עד שיתווסף לו קוד, אז העדיפו שינוי של חוק קיים כשאפשר.

כללים:
- הציעו רק שינויים שההסכם באמת קובע. אל תמציאו ערכים או תאריכים. אם ההסכם תואם את המצב הקיים - אין שינוי.
- לכל שינוי: explanation בעברית (מה משתנה ולמה), section (מספר הסעיף בהסכם אם יש), quote (ציטוט קצר ומדויק מההסכם שמבסס את השינוי).
- תאריכים בפורמט YYYY-MM-DD. תחילת שנת לימודים היא 01/09. ערך בוליאני בתנאי: "1" או "0". ערך לבחירה: הקוד מתוך הרשימה. "in": ערכים מופרדים בפסיקים.
- שדות ופעולות רק מתוך אוצר המילים שלמטה. סעיף רלוונטי לגמולים שלא ניתן לייצג כך - הכניסו ל-not_mapped עם הסבר.
- summary: תקציר בעברית של עיקרי ההסכם (עד 6 משפטים). agreement_title: שם ההסכם כפי שמופיע בו.
- שדות שלא רלוונטיים לסוג השינוי: null.
"""


def _vocabulary_text():
    lines = ["שדות לתנאים (field: תווית, סוג):"]
    for code, (label, ftype, _) in FIELDS.items():
        extra = f" — ערכים: {', '.join(f'{k}={v}' for k, v in CHOICES[code].items())}" if code in CHOICES else ""
        lines.append(f"- {code}: {label} ({ftype}){extra}")
    lines.append("השוואות (operator): " + ", ".join(f"{k} ({v[0]})" for k, v in OPERATORS.items()))
    lines.append("מחברים (connector): AND, OR")
    lines.append("פעולות (action):")
    for group, actions in ACTIONS.items():
        for code, label in actions.items():
            lines.append(f"- {code}: {label} [{group}]")
    return "\n".join(lines)


def constitutions_context(conn):
    """כל החוקות במצבן הנוכחי, כ-JSON קריא - ההקשר ש-Claude משווה אליו את ההסכם."""
    out = []
    for c in conn.execute("SELECT * FROM constitutions ORDER BY constitution_id").fetchall():
        rules = conn.execute(
            "SELECT * FROM rules WHERE constitution_id=%s ORDER BY priority, rule_id", (c["constitution_id"],)
        ).fetchall()
        rule_list = []
        for r in rules:
            conds = conn.execute(
                "SELECT connector, field, operator, value, value_field FROM rule_conditions "
                "WHERE rule_id=%s ORDER BY seq, condition_id", (r["rule_id"],)).fetchall()
            params = conn.execute(
                "SELECT param_name, param_value, valid_from, valid_to FROM rule_parameters "
                "WHERE rule_id=%s ORDER BY param_name, valid_from NULLS FIRST", (r["rule_id"],)).fetchall()
            rule_list.append({
                "rule_id": r["rule_id"], "priority": r["priority"], "title": r["title"],
                "description": r["description"], "source_doc": r["source_doc"], "source_ref": r["source_ref"],
                "applies_when": r["applies_when"], "valid_from": r["valid_from"], "valid_to": r["valid_to"],
                "conditions": [dict(x) for x in conds], "action": r["action"], "result_value": r["result_value"],
                "parameters": [dict(p) for p in params],
            })
        out.append({"constitution_id": c["constitution_id"], "name": c["name"],
                    "description": c["description"], "rules": rule_list})
    return json.dumps(out, ensure_ascii=False, indent=1)


def _docx_text(data):
    """טקסט מקובץ Word (docx), פסקה לשורה, כולל טבלאות. בלי תלות נוספת."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8")
    except (zipfile.BadZipFile, KeyError):
        raise AgreementError("קובץ ה-Word פגום או לא בפורמט docx.")
    paras = re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S)
    texts = ["".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p)) for p in paras]
    text = "\n".join(t for t in texts if t.strip())
    for ent, ch in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&")):
        text = text.replace(ent, ch)
    return text


def document_block(filename, data):
    """בלוק התוכן של ההסכם לבקשה ל-Claude, לפי סוג הקובץ."""
    if len(data) > MAX_FILE_BYTES:
        raise AgreementError("הקובץ גדול מדי (עד 25MB).")
    name = (filename or "").lower()
    if name.endswith(".pdf") or data[:5] == b"%PDF-":
        return {"type": "document", "title": filename,
                "source": {"type": "base64", "media_type": "application/pdf",
                           "data": base64.standard_b64encode(data).decode("ascii")}}
    if name.endswith(".docx"):
        text = _docx_text(data)
    elif name.endswith(".txt"):
        text = data.decode("utf-8", errors="replace")
    else:
        raise AgreementError("סוג קובץ לא נתמך. אפשר לטעון PDF, Word (docx) או טקסט (txt).")
    if not text.strip():
        raise AgreementError("לא נמצא טקסט בקובץ.")
    return {"type": "document", "title": filename,
            "source": {"type": "text", "media_type": "text/plain", "data": text}}


def analyze_agreement(conn, filename, data, client=None):
    """
    שולח את ההסכם ל-Claude ומחזיר dict לפי OUTPUT_SCHEMA. מעלה AgreementError
    עם הודעה בעברית כשמשהו נכשל. client ניתן להחלפה לבדיקות.
    """
    doc = document_block(filename, data)
    if client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise AgreementError("לא הוגדר ANTHROPIC_API_KEY בשרת - אי אפשר לנתח הסכמים.")
        import anthropic
        client = anthropic.Anthropic()

    context = constitutions_context(conn)
    user_text = (
        "אוצר המילים של החוקה הטבלאית:\n" + _vocabulary_text()
        + "\n\nהחוקות במצבן הנוכחי (JSON):\n" + context
        + "\n\nההסכם מצורף למעלה. הציעו את השינויים בחוקות שההסכם מחייב."
    )
    import anthropic
    try:
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=64000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": [doc, {"type": "text", "text": user_text}]}],
        ) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise AgreementError("מפתח ה-API של Anthropic שגוי (ANTHROPIC_API_KEY).")
    except anthropic.RateLimitError:
        raise AgreementError("שירות הניתוח עמוס כרגע. נסי שוב בעוד כמה דקות.")
    except anthropic.BadRequestError as exc:
        raise AgreementError(f"הבקשה לשירות הניתוח נדחתה: {exc.message}")
    except anthropic.APIStatusError as exc:
        raise AgreementError(f"שגיאה בשירות הניתוח ({exc.status_code}). נסי שוב.")
    except anthropic.APIConnectionError:
        raise AgreementError("אין חיבור לשירות הניתוח. נסי שוב.")

    if message.stop_reason == "refusal":
        raise AgreementError("שירות הניתוח סירב לקרוא את המסמך.")
    if message.stop_reason == "max_tokens":
        raise AgreementError("ההסכם ארוך מדי לניתוח בבת אחת.")
    text = next((b.text for b in message.content if b.type == "text"), "")
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        raise AgreementError("התשובה של שירות הניתוח לא הייתה תקינה. נסי לנתח שוב.")
    return result
