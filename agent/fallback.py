"""
רשת הביטחון: אותה שיחה, בסדר קבוע בקוד, בלי AI.

רצה כשאין מפתח API, כשהשירות נכשל, כשהסוכן נתקע (כלל 9), או כשהתשובה שלו
נכשלת בבדיקת המספרים. מחזירה את אותו חוזה ואותם כרטיסים, עם fallback=True,
והטקסט נכתב מתבניות קבועות עם המספרים מהכלים. הדמו לא נופל.

הסדר: קבצים חדשים ← תשובה לשאלה ממתינה ← מה חסר: קובץ חסר (הדרכה) או שאלה
(כפתורים) ← אם אפשר: בדיקה ← אם יש פער: פניות.
"""
import payslips

from agent import case as case_mod
from agent import tools
from agent.loop import finish

REASON_NOTE = {
    "no_api_key": None,
    "api_error": "השירות של ה-AI לא זמין כרגע, אז המשכתי בלעדיו.",
    "refusal": None,
    "stuck": None,
    "guard": None,
}


def _u(x):
    return payslips._fmt(x)


def run_fallback(conn, tid, case, messages, event, extract_payslip=None, reason="no_api_key",
                 prior_did=None, prior_cards=None):
    ctx = {"conn": conn, "teacher_id": tid, "case": case, "extract_payslip": extract_payslip}
    did, cards = list(prior_did or []), list(prior_cards or [])
    kind = (event or {}).get("type")
    text_parts = []

    # --- 1. קבצים חדשים ---
    if kind == "files":
        for doc_id in event.get("doc_ids") or []:
            res, c, d, _ = tools.execute(ctx, "read_file", {"doc_id": doc_id})
            cards += c
            if d:
                did.append(d)
        unknown = [c for c in cards if c["type"] == "file" and c["doc_type"] == "unknown"]
        failed = [c for c in cards if c["type"] == "file" and not c["ok"] and c["doc_type"] != "unknown"]
        if unknown:
            text_parts.append(f"הקובץ {unknown[0]['label']} לא שייך לבדיקה, אז לא שמרתי אותו." if len(unknown) == 1
                              else "כמה מהקבצים לא שייכים לבדיקה, אז לא שמרתי אותם.")
        if failed:
            text_parts.append(f"לא הצלחתי לקרוא את {failed[0]['label']}. אפשר לנסות צילום ברור יותר, או PDF מהפורטל.")

    # --- 2. תשובה לשאלה ממתינה ---
    pending = case.get("pending")
    if pending and kind == "answer":
        res = tools.save_answer(conn, tid, case, pending, event.get("value"))
        if res.get("saved"):
            did.append("שמרתי: " + (res.get("label") or "התשובה"))
        case["pending"] = None
        case.pop("pending_results", None)
    elif pending and kind in (None, "resume"):
        return finish(conn, tid, case, messages, "question", "", [pending.get("card") or {}], did, fallback=True)

    if kind == "sent":
        res, c, d, _ = tools.execute(ctx, "set_reminder", {"note": "תלוש הבא"})
        did.append(d)
        return finish(conn, tid, case, messages, "reply",
                      "מעולה. אזכיר לך כשיגיע התלוש הבא. גררי אותו לכאן ואבדוק לבד אם התיקון נכנס.",
                      cards, did, fallback=True)

    if kind == "message":
        # בלי AI אין שאלה חופשית. מפנים לתשובות המוכנות
        res, c, d, is_err = tools.execute(ctx, "explain", {"rule_id": None})
        qs = (res or {}).get("saved_questions") or []
        if qs and not is_err:
            q = qs[0]
            return finish(conn, tid, case, messages, "reply",
                          f"{q['a']}" + (f" ({q['cite']})" if q.get("cite") else ""), cards, did, fallback=True)
        return finish(conn, tid, case, messages, "reply",
                      "על השאלה הזו אני לא יכולה לענות כרגע. אפשר לברר במוקד חשבות השכר ‎*6552.",
                      cards, did, fallback=True)

    # --- 3. מה חסר ---
    res, _, d, _ = tools.execute(ctx, "case_status", {})
    did.append(d)
    st = ctx.get("status") or {}
    missing = st.get("missing") or []
    files_missing = [m for m in missing if m["kind"] == "file"]
    questions = [m for m in missing if m["kind"] == "question"]

    if not st.get("ready_to_check"):
        if files_missing:
            m = files_missing[0]
            _, c, d, _ = tools.execute(ctx, "portal_guide", {"doc_type": m["doc_type"]})
            cards += c
            did.append(d)
            label = c[0]["label"] if c else m["doc_type"]
            if not text_parts and not case.get("docs"):
                text_parts.append("שלום. גררי לכאן את הקבצים שיש לך מהפורטל: התלוש, הקורסים שלי וההצטרפות לעוז לתמורה. לא צריך לסמן מה כל קובץ.")
            else:
                text_parts.append(f"עכשיו צריך את {label}. {m['why']}")
            return finish(conn, tid, case, messages, "reply", " ".join(text_parts), cards, did, fallback=True)

    # שאלה רק כשהיא משנה את התוצאה, ועד 3 בכל התהליך
    if questions and case.get("questions_asked", 0) < tools.MAX_QUESTIONS:
        q = questions[0]
        inp = {"field": q["field"], "school_year": q.get("school_year"), "question": q["text"], "options": q["options"]}
        card = tools.question_card(inp, st)
        case["pending"] = dict(inp, id=None, card=card)
        case["questions_asked"] = case.get("questions_asked", 0) + 1
        cards.append(card)
        text_parts.append("קראתי את הקבצים. יש לי שאלה אחת:" if kind == "files" else "יש לי שאלה אחת:")
        return finish(conn, tid, case, messages, "question", " ".join(text_parts), cards, did, fallback=True)

    # --- 4. בדיקה ---
    res, c, d, is_err = tools.execute(ctx, "run_check", {"what_if": None})
    if is_err:
        text_parts.append("לא הצלחתי להריץ את הבדיקה כרגע. נסי שוב בעוד דקה.")
        return finish(conn, tid, case, messages, "error", " ".join(text_parts), cards, did, fallback=True)
    cards += c
    did.append(d)
    if res["ok"]:
        text_parts.append(f"בדקתי: בתלוש של {payslips.month_label(res['pay_month'])} מוכרות {_u(res['paid_units'])} יחידות, "
                          "בדיוק כמו שמגיע לך לפי החוקה. אין מה לעשות כרגע.")
        return finish(conn, tid, case, messages, "reply", " ".join(text_parts), cards, did, fallback=True)

    # --- 5. פניות ---
    pres, c, d, _ = tools.execute(ctx, "plan_actions", {})
    cards += c
    if d:
        did.append(d)
    gap = res["gap_units"]
    if gap > 0:
        line = f"בתלוש חסרות לך {_u(gap)} יחידות"
        if res.get("gap_ils_monthly"):
            line += f", כ-{_u(round(res['gap_ils_monthly']))} ₪ בחודש"
        text_parts.append(line + ".")
    else:
        text_parts.append(f"בתלוש מוכרות {_u(-gap)} יחידות יותר ממה שחישבנו. כנראה חסרים אצלנו נתונים.")
    n = len(c)
    if n == 2:
        text_parts.append("הכנתי שתי פניות. כדאי לשלוח את שתיהן עכשיו, במקביל.")
    elif n > 2:
        text_parts.append(f"הכנתי {n} פניות. כדאי לשלוח את כולן עכשיו, במקביל.")
    elif n == 1:
        text_parts.append("הכנתי את הפנייה. אפשר לשלוח אותה עכשיו.")
    note = REASON_NOTE.get(reason)
    if note:
        text_parts.insert(0, note)
    return finish(conn, tid, case, messages, "reply", " ".join(text_parts), cards, did, fallback=True)
