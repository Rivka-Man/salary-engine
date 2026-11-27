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

# שלושת הקבצים שבלעדיהם אין חישוב, בסדר שבו מבקשים אותם (קובץ אחד בכל שלב)
CORE_FILES = ["payslip", "oz_join", "courses"]

OUT_OF_SCOPE = "זה מחוץ למה שאני בודקת. אפשר לפנות למוקד חשבות השכר \u200e*6552."
DONT_KNOW_TEXT = "אני לא יודעת את זה. כדאי לשאול את מוקד חשבות השכר \u200e*6552."
# נושאים שמחוץ לגמול ההשתלמות בעוז לתמורה
_OFF_TOPIC = ("מס הכנסה", "נקודות זיכוי", "פנסיה", "קרן השתלמות", "אופק חדש", "הבראה", "ביטוח לאומי",
              "ביגוד", "נסיעות", "חופשת לידה", "פיטורים", "פרישה", "ותק")
# מילים שמסמנות שהשאלה בתחום
_ON_TOPIC = ("גמול", "השתלמות", "קורס", "שבתון", "חל\"ת", "חלת", "עוז", "יחיד", "תלוש", "249", "פנייה", "פניה", "שעות")


def _words(t):
    return {w for w in "".join(ch if ch.isalnum() or ch in '"' else " " for ch in (t or "")).split() if len(w) >= 3}


def answer_message(text, saved_questions):
    """
    תשובה לשאלה חופשית בלי AI. רק מתוך השאלות השמורות, עם הסעיף. אין התאמה: "לא יודעת".
    מחזיר טקסט.
    """
    t = text or ""
    if any(k in t for k in _OFF_TOPIC) and not any(k in t for k in ("גמול", "השתלמות", "קורס", "שבתון")):
        return OUT_OF_SCOPE
    if not any(k in t for k in _ON_TOPIC):
        return OUT_OF_SCOPE
    words = _words(t)
    best, score = None, 0
    for q in saved_questions or []:
        sc = len(words & _words(q.get("q", "")))
        if sc > score:
            best, score = q, sc
    if best and score >= 2 and best.get("cite"):
        return f"{best['a']} ({best['cite']})"
    return DONT_KNOW_TEXT


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
        if res.get("unclear"):  # לא מנחשים: שואלים שוב, עם הכפתורים
            return finish(conn, tid, case, messages, "question", "לא הייתי בטוחה שהבנתי. אפשר לבחור אחת מהאפשרויות:",
                          [pending.get("card") or {}], did, fallback=True)
        if res.get("saved"):
            did.append("שמרתי: " + (res.get("label") or "התשובה"))
        if res.get("need_file") == "vacations":
            # "לא יודעת": הדרכה איפה בודקים, ואותה שאלה שוב (לא תוצאה)
            _, c, d, _ = tools.execute(ctx, "portal_guide", {"doc_type": "vacations"})
            cards += c + [pending.get("card") or {}]
            did.append(d)
            return finish(conn, tid, case, messages, "question",
                          f"אין בעיה. זה כתוב בפורטל, בעמוד \"חל\"ת ושבתון\". אחרי שבדקת, ענייני כאן:",
                          cards, did, fallback=True)
        case["pending"] = None
        case.pop("pending_results", None)
        if res.get("need_file"):
            _, c, d, _ = tools.execute(ctx, "portal_guide", {"doc_type": res["need_file"]})
            cards += c
            did.append(d)
            if res["need_file"] == "vacations":
                text = (f"אין בעיה. אם {res['school_year']} הייתה שנת שבתון או חל\"ת רואים בעמוד \"חל\"ת ושבתון\" בפורטל. "
                        "הנה בדיוק איך מגיעים אליו. גררי לכאן את ה-PDF, ואבדוק לבד.")
            else:
                text = ("אין בעיה. אם מוכר לך כפל תואר רואים בעמוד הגמולים בפורטל. "
                        "הנה בדיוק איך מגיעים אליו. גררי לכאן את ה-PDF, ואבדוק לבד.")
            return finish(conn, tid, case, messages, "reply", text, cards, did, fallback=True)
    elif pending and kind in (None, "resume"):
        return finish(conn, tid, case, messages, "question", "", [pending.get("card") or {}], did, fallback=True)

    if kind == "sent":
        names = {"payroll": "חשבות השכר", "institution": "מוסד ההשתלמות", "ministry": "המשרד", "unique": "יוניק"}
        ref = (case.get("sent_info") or {}).get(event.get("track"), {}).get("ref")
        did.append(f"רשמתי: נשלח ל{names.get(event.get('track'), 'הגורם')}" + (f" · פנייה {ref}" if ref else ""))
        left = [t for t in (case.get("plan") or {}).get("tracks", []) if t not in case.get("sent", [])]
        if left:
            return finish(conn, tid, case, messages, "reply",
                          f"רשמתי. נשארה עוד פנייה אחת, ל{names.get(left[0], 'גורם נוסף')}. כדאי לשלוח אותה עכשיו, במקביל.",
                          cards, did, fallback=True)
        res, c, d, _ = tools.execute(ctx, "set_reminder", {"note": "תלוש הבא"})
        cards += c
        did.append(d)
        return finish(conn, tid, case, messages, "reply",
                      "מעולה, הפניות יצאו. הוסיפי את התזכורת ליומן: ב-10 לחודש הבא נבדוק יחד אם התיקון נכנס לתלוש.",
                      cards, did, fallback=True)

    if kind == "message":
        # בלי AI: עונים רק מתוך השאלות השמורות, עם סעיף. אחרת "לא יודעת" או "מחוץ לתחום"
        res, c, d, is_err = tools.execute(ctx, "explain", {"rule_id": None})
        qs = (res or {}).get("saved_questions") or [] if not is_err else []
        return finish(conn, tid, case, messages, "reply", answer_message(event.get("text"), qs),
                      cards, did, fallback=True)

    # --- 3. מה חסר ---
    res, _, d, _ = tools.execute(ctx, "case_status", {})
    did.append(d)
    st = ctx.get("status") or {}
    missing = st.get("missing") or []
    files_missing = [m for m in missing if m["kind"] == "file"]
    questions = [m for m in missing if m["kind"] == "question"]

    # כל קובץ חסר עוצר את הבדיקה: בלי "הקורסים שלי" (או עוז, או החופשות) התוצאה עלולה להטעות
    blocking = sorted(files_missing, key=lambda m: CORE_FILES.index(m["doc_type"]) if m["doc_type"] in CORE_FILES else 9)
    if blocking:
        m = blocking[0]
        _, c, d, _ = tools.execute(ctx, "portal_guide", {"doc_type": m["doc_type"]})
        cards += c
        did.append(d)
        label = c[0]["label"] if c else m["doc_type"]
        if not text_parts and not case.get("docs"):
            text_parts.append("שלום. גררי לכאן את הקבצים שיש לך מהפורטל: התלוש, הקורסים שלי וההצטרפות לעוז לתמורה. לא צריך לסמן מה כל קובץ.")
        else:
            text_parts.append(f"עכשיו צריך את קובץ {label}. {m['why']} גררי אותו לכאן.")
        return finish(conn, tid, case, messages, "reply", " ".join(text_parts), cards, did, fallback=True, st=st)

    # שאלה רק כשהיא משנה את התוצאה, ועד 3 בכל התהליך
    if questions and case.get("questions_asked", 0) < tools.MAX_QUESTIONS:
        q = questions[0]
        inp = {"field": q["field"], "school_year": q.get("school_year"), "question": q["text"], "options": q["options"]}
        card = tools.question_card(inp, st)
        case["pending"] = dict(inp, id=None, card=card)
        case["questions_asked"] = case.get("questions_asked", 0) + 1
        cards.append(card)
        text_parts.append("קראתי את הקבצים. יש לי שאלה אחת:" if kind == "files" else "יש לי שאלה אחת:")
        return finish(conn, tid, case, messages, "question", " ".join(text_parts), cards, did, fallback=True, st=st)

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
    n = len([x for x in c if x.get("type") == "action"])  # בלי כרטיס התזכורת
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
