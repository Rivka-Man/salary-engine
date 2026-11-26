"""
"למה?" בצ'אט: שאלות של המורה על התוצאה שלה.

שני סוגי שאלות:
1. שאלות שמורות (saved_questions) - נבנות מהתוצאה עצמה ונענות בלי AI, מתוך
   החוקים, ה-trace וההשוואה. תמיד זמינות, וזהות בכל הרצה.
2. שאלה חופשית (ask_free) - Claude עונה, אבל רק מתוך העובדות שהמנוע כבר
   חישב (facts). הוא לא מחשב מספרים חדשים: מסביר ומצטט את הסעיף.
"""
import json
import os

from payslips import MODEL, _fmt, _units

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "cite": {"type": "string"},
    },
    "required": ["answer", "cite"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """את/ה העוזר/ת של OptiPay, שמסביר/ה למורה בעוז לתמורה את בדיקת גמול ההשתלמות שלה.

כללים:
- עונים רק מתוך העובדות שב-facts (תוצאת מנוע החוקים, הרכיבים, החוקים והסעיפים). לא מחשבים מספרים חדשים ולא משערים.
- אם השאלה לא קשורה לגמול ההשתלמות או שהתשובה לא נמצאת ב-facts: אומרים זאת בפשטות, ומפנים למוקד חשבות השכר ‎*6552.
- עברית פשוטה, פנייה בלשון נקבה, עד 4 משפטים. בלי כותרות ובלי רשימות.
- למה לעשות ומי לפנות - מפנים לכפתור "מה עליי לעשות?".
- cite: הסעיף שהתשובה נשענת עליו כפי שהוא מופיע ב-facts (למשל "הסכם עוז לתמורה, סעיף 18(ב)"), או מחרוזת ריקה."""


class AskError(Exception):
    """שגיאה עם הודעה בעברית שמוצגת למורה."""


def _src(rule):
    if not rule:
        return ""
    doc = (rule["source_doc"] or "").replace("_", " ")
    return f"{doc}, {rule['source_ref']}" if rule["source_ref"] else doc


def facts(cmp, result, rules):
    """העובדות שהצ'אט רשאי להשתמש בהן - בלי פרטים אישיים."""
    used = {c.get("rule_id") for c in cmp["components"] if c.get("rule_id")}
    used |= {t.get("rule_id") for t in result.get("trace") or []}
    return {
        "paid_units": cmp["paid_units"], "expected_units": cmp["expected_units"],
        "gap_units": cmp["gap_units"], "gap_ils_monthly": cmp["gap_ils"],
        "unit_pct_of_salary": cmp["rate"], "line_missing": cmp["line_missing"],
        "diagnosis": {"title": cmp["cause"]["title"], "text": cmp["cause"]["text"]},
        "components": [{"label": c["label"], "units": c["units"], "missing_in_payslip": bool(c.get("flagged")),
                        "rule_id": c.get("rule_id")} for c in cmp["components"]],
        "hours_per_unit": result.get("H_unit"), "units_cap": result.get("U_cap_applied"),
        "hours_carried_to_next_unit": result.get("carry_remaining"),
        "calc_date": result.get("calc_date"),
        "trace": [{"rule_id": t.get("rule_id"), "message": t.get("message"), "value": t.get("value")}
                  for t in result.get("trace") or []],
        "rules": {rid: {"title": rules[rid]["title"], "description": rules[rid]["description"], "source": _src(rules[rid])}
                  for rid in used if rid in rules},
    }


def saved_questions(cmp, result, rules, status_by_year=None):
    """2-3 שאלות שמורות שמתאימות לתוצאה הזו, כל אחת עם תשובה מוכנה."""
    out = []
    # שנת שבתון / חל"ת: השאלה הנפוצה ביותר (מהישיבה), עם התשובה מה-trace של המנוע
    special = [(y, s) for y, s in sorted((status_by_year or {}).items()) if s != "רגיל"]
    if special:
        y, s = special[-1]
        msg = next((t["message"] for t in result.get("trace") or []
                    if t.get("rule_id") == "R12_SABBATICAL_EXEMPT" and y in (t.get("message") or "")), "")
        r = rules.get("R12_SABBATICAL_EXEMPT")
        a = (f"בשנה רגילה מוכרת לכל היותר יחידת גמול אחת בשנה, גם אם למדת יותר. "
             f"בשנת {s} התקרה הזו לא חלה, וכל השעות נספרות.")
        if msg:
            a += f" אצלך, ב{y}: {msg.split(' - ', 1)[-1]}."
        out.append({"id": "sabbatical", "q": f"איך ה{s} ב{y} משפיע?", "a": a, "cite": _src(r)})
    flagged = [c for c in cmp["components"] if c.get("flagged")]
    for c in flagged[:1]:
        r = rules.get(c.get("rule_id")) if c.get("rule_id") else None
        msg = next((t["message"] for t in result.get("trace") or [] if t.get("rule_id") == c.get("rule_id")), "")
        ans = f"{c['label']} שווה לך {_units(c['units'])}."
        if r:
            ans += f" לפי החוק \"{r['title']}\": {r['description']}"
        if msg:
            ans += f" בחישוב שלך: {msg}."
        ans += f" בתלוש מוכרות {_fmt(cmp['paid_units'])} יחידות, והרכיב הזה לא נכלל בהן."
        out.append({"id": f"why:{c['key']}", "q": f"למה {c['label']} לא בתלוש?", "a": ans, "cite": _src(r)})
    if not cmp["ok"] and cmp["gap_units"] > 0:
        if cmp["gap_ils"] is not None:
            a = (f"כל יחידת גמול שווה {_fmt(cmp['rate'])}% מהשכר שלך בחלק המשרה שבעוז לתמורה. "
                 f"{_units(cmp['gap_units'])} הן בערך {_fmt(cmp['gap_ils'], 0)} ₪ בכל חודש, "
                 "ובפנייה מבקשים גם הפרשים מהמועד שבו נוצרה הזכאות.")
        else:
            a = (f"כל יחידת גמול שווה {_fmt(cmp['rate'])}% מהשכר שלך בחלק המשרה שבעוז לתמורה, "
                 f"כלומר {_fmt(round(cmp['gap_units'] * cmp['rate'], 4))}% תוספת לשכר. הסכום בשקלים תלוי בשכר, ולכן משווים יחידות.")
        out.append({"id": "ils", "q": "כמה זה בשקלים?", "a": a, "cite": ""})
    h, cap = result.get("H_unit"), result.get("U_cap_applied")
    if h:
        a = f"כל {_fmt(h)} שעות השתלמות מוכרות הן יחידת גמול אחת."
        if result.get("carry_remaining"):
            a += f" כבר צברת {_fmt(result['carry_remaining'])} שעות לקראת היחידה הבאה."
        if cap:
            a += f" התקרה שחלה עלייך היא {_fmt(cap)} יחידות רגילות, וכרגע מוכרות לך {_fmt(result.get('U_regular'))}."
        r = rules.get("R01_UNIT_HOURS")
        out.append({"id": "more", "q": "מה אם אשלים עוד קורס השנה?", "a": a, "cite": _src(r)})
    return out[:3]


def ask_free(question, fact_dict, client=None):
    """שאלה חופשית: Claude עונה מתוך העובדות. מחזיר {"answer", "cite"}."""
    question = (question or "").strip()
    if not question:
        raise AskError("כתבי שאלה.")
    if len(question) > 500:
        raise AskError("השאלה ארוכה מדי. נסי לקצר.")
    if client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise AskError("שאלות חופשיות לא זמינות כרגע. אפשר לבחור באחת השאלות השמורות.")
        import anthropic
        client = anthropic.Anthropic()
    import anthropic
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "facts:\n" + json.dumps(fact_dict, ensure_ascii=False)},
                {"type": "text", "text": "השאלה של המורה:\n" + question},
            ]}],
        )
    except anthropic.RateLimitError:
        raise AskError("השירות עמוס כרגע. נסי שוב בעוד דקה.")
    except anthropic.APIError:
        raise AskError("לא הצלחתי לענות כרגע. אפשר לבחור באחת השאלות השמורות.")
    text = next((b.text for b in message.content if b.type == "text"), "")
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        raise AskError("לא הצלחתי לענות כרגע. אפשר לבחור באחת השאלות השמורות.")
    return {"answer": out.get("answer") or "", "cite": out.get("cite") or ""}
