"""
לולאת הסוכן: run_agent(teacher_id, event).

אין סדר שלבים בקוד. יש תיק (agent_cases), ואחרי כל פעולה של המורה Claude
מחליט אילו כלים להפעיל. הלולאה נעצרת כשיש תשובה למורה, או כשהסוכן שואל שאלה
(ask_teacher), ואז השיחה נשמרת וממשיכה כשהמורה עונה, גם בבקשת HTTP אחרת.

לולאה ידנית (ולא tool runner) כי צריך לעצור באמצע, לשמור את ההיסטוריה ל-DB,
ולהמשיך ממנה בבקשה הבאה.

רשת ביטחון (fallback.py) עונה במקום Claude כשאין מפתח API, כשהשירות נכשל,
כשהסוכן מפעיל 6 כלים ברצף בלי לענות, או כשהתשובה שלו נכשלת בבדיקת המספרים.
"""
import json
import os

from db import get_connection

from agent import case as case_mod
from agent import guard
from agent import store
from agent import tools

MODEL = "claude-opus-5-5"
MAX_TOOLS_WITHOUT_REPLY = 6   # כלל 9 באפיון
MAX_API_CALLS = 10
PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt.md")


def _system_prompt():
    with open(PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


def _dump(block):
    """בלוק תוכן מהתשובה ← dict ששומרים ומחזירים כמו שהוא (כולל thinking עם החתימה)."""
    if isinstance(block, dict):
        return block
    if hasattr(block, "to_dict"):
        return block.to_dict()
    return block.model_dump(mode="json", exclude_none=True)


def _sanitize(messages):
    """היסטוריה שנקטעה באמצע (תשובה עם tool_use בלי tool_result) - חותכים את הסוף."""
    while messages and messages[-1]["role"] == "assistant" and any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in messages[-1]["content"]):
        messages.pop()
    return messages


# ------------------------------------------------------------------
# האירוע של המורה ← הודעת user
# ------------------------------------------------------------------

def _doc_names(conn, tid, ids):
    if not ids:
        return {}
    rows = conn.execute("SELECT doc_id, filename FROM teacher_documents WHERE teacher_id=%s AND doc_id = ANY(%s)",
                        (tid, list(ids))).fetchall()
    return {r["doc_id"]: r["filename"] for r in rows}


def _event_text(conn, tid, case, event):
    kind = (event or {}).get("type") or "resume"
    if kind == "files":
        ids = event.get("doc_ids") or []
        names = _doc_names(conn, tid, ids)
        files = ", ".join(f"doc_id={i} ({names.get(i, '?')})" for i in ids)
        return f"המורה גררה {len(ids)} קבצים: {files}."
    if kind == "answer":
        return f"המורה ענתה: {event.get('value')}"
    if kind == "message":
        return f"המורה כתבה: {(event.get('text') or '')[:500]}"
    if kind == "sent":
        ref = f", מספר פנייה {event['ref']}" if event.get("ref") else ""
        return f"המורה סימנה שהפנייה ({event.get('track')}) נשלחה{ref}. אם כל הפניות נשלחו: set_reminder."
    if not case.get("result") and not case.get("docs"):
        return "המורה נכנסה לשיחה בפעם הראשונה. תני פתיחה קצרה ובקשי את הקבצים."
    return "המורה חזרה לקישור. אמרי לה במשפט אחד איפה היא עומדת ומה הצעד הבא."


# ------------------------------------------------------------------
# מה עשיתי + התשובה למורה
# ------------------------------------------------------------------

def summarize_did(did):
    reads = [d for d in did if d in ("read_ok", "read_unknown", "read_failed")]
    out = []
    if reads:
        out.append(f"קראתי {len(reads)} קבצים" if len(reads) > 1 else "קראתי קובץ")
        unk = reads.count("read_unknown")
        if unk:
            out.append("קובץ אחד לא שייך" if unk == 1 else f"{unk} קבצים לא שייכים")
        fail = reads.count("read_failed")
        if fail:
            out.append("קובץ אחד לא נקרא" if fail == 1 else f"{fail} קבצים לא נקראו")
    for d in did:
        if d and d not in reads and d not in out:
            out.append(d)
    return out


def order_cards(cards):
    rank = {"file": 0, "result": 1, "action": 2, "reminder": 3, "guide": 4, "question": 5}
    seen, out = set(), []
    for c in cards:
        k = (c["type"], c.get("doc_id"), c.get("track"), c.get("field"), c.get("doc_type") if c["type"] == "guide" else None)
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    # כרטיס תוצאה של בדיקה מאוחרת מחליף את הקודם
    results = [c for c in out if c["type"] == "result"]
    if len(results) > 1:
        out = [c for c in out if c["type"] != "result"] + [results[-1]]
    return sorted(out, key=lambda c: rank.get(c["type"], 9))


def case_view(conn, tid, case, st=None):
    st = st or case_mod.case_status(conn, tid, case)
    facts = [{"key": k["key"], "label": k["label"], "value": k["value"], "source": k["source"]}
             for k in st["known"] if k["key"] != "paid_units"]
    sent_info = case.get("sent_info") or {}
    return {"docs": st["docs"], "facts": facts, "result": case.get("result"),
            "sent": case.get("sent") or [],
            "sent_info": [{"track": t, **sent_info.get(t, {})} for t in case.get("sent") or []],
            "reminder": case.get("reminder"),
            "progress": 100 if case.get("result") else case_mod.progress(st)}


def finish(conn, tid, case, messages, status, text, cards, did, fallback=False, st=None):
    """st: case_status שכבר חושב בתור הזה, כשלא נכתב כלום מאז. חוסך 5 שאילתות."""
    out = {"status": status, "did": summarize_did(did), "text": text, "cards": order_cards(cards),
           "case": case_view(conn, tid, case, st), "fallback": fallback}
    store.log_event(conn, tid, "fallback" if fallback else "reply", status, None,
                    {"text": text, "did": out["did"], "cards": [c["type"] for c in out["cards"]]})
    store.save_case(conn, tid, case, messages)  # ה-commit היחיד בתור, כולל היומן
    return out


# ------------------------------------------------------------------
# הלולאה
# ------------------------------------------------------------------

def _client():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic
    return anthropic.Anthropic()


def _call(client, messages):
    return client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=_system_prompt(),
        tools=tools.TOOL_DEFS,
        messages=messages,
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )


def _text_of(content):
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()


def run_agent(teacher_id, event=None, client=None, extract_payslip=None):
    """
    נקודת הכניסה של המסך. event: ראו agent/contracts.py.
    client / extract_payslip ניתנים להחלפה בבדיקות.
    """
    conn = get_connection()
    try:
        case, messages = store.load_case(conn, teacher_id)
        store.log_event(conn, teacher_id, "event", (event or {}).get("type"), event)
        if (event or {}).get("type") == "sent" and event.get("track"):
            if event["track"] not in case["sent"]:
                case["sent"].append(event["track"])
            # מספר הפנייה (לא חובה) ותאריך השליחה, ל"התיק שלי" ולתזכורת המעקב
            case.setdefault("sent_info", {})[event["track"]] = {
                "ref": (str(event.get("ref") or "").strip()[:40] or None),
                "on": store._now()[:10]}
        client = client or _client()
        if client is None:
            from agent.fallback import run_fallback
            return run_fallback(conn, teacher_id, case, messages, event, extract_payslip, reason="no_api_key")
        return _run(conn, teacher_id, case, _sanitize(messages), event, client, extract_payslip)
    finally:
        conn.close()


def _run(conn, tid, case, messages, event, client, extract_payslip):
    import anthropic
    from agent.fallback import run_fallback

    ctx = {"conn": conn, "teacher_id": tid, "case": case, "extract_payslip": extract_payslip}
    did, cards, outputs = [], [], list(case.get("tool_outputs") or [])[-40:]
    pending = case.get("pending")
    kind = (event or {}).get("type")

    # חזרה לקישור כשיש שאלה פתוחה: מציגים אותה שוב, בלי לקרוא ל-Claude
    if pending and kind in (None, "resume"):
        return finish(conn, tid, case, messages, "question", "", [pending.get("card") or {}], [])

    base_len = len(messages)

    def to_fallback(reason):
        del messages[base_len:]  # מה שנוסף בתור הזה לא נשמר; רשת הביטחון ממשיכה מהתיק
        return run_fallback(conn, tid, case, _sanitize(messages), None, extract_payslip, reason=reason,
                            prior_did=did, prior_cards=cards)

    # --- ההודעה של המורה. אם יש שאלה ממתינה, היא נענית כ-tool_result ---
    content = []
    if pending:
        if kind == "answer":
            res = tools.save_answer(conn, tid, case, pending, event.get("value"))
            if res.get("saved"):
                did.append("שמרתי: " + (res.get("label") or "התשובה"))
        else:
            res = {"answer": None, "note": "המורה לא ענתה על השאלה, ובמקום זה: " + _event_text(conn, tid, case, event)}
        outputs.append(res)
        if pending.get("id"):
            content += case.pop("pending_results", []) + [
                {"type": "tool_result", "tool_use_id": pending["id"], "content": json.dumps(res, ensure_ascii=False)}]
        else:
            content.append({"type": "text", "text": "התשובה לשאלה ששאלת: " + json.dumps(res, ensure_ascii=False)})
        case["pending"] = None
    if not content or kind not in ("answer", None, "resume"):
        content.append({"type": "text", "text": _event_text(conn, tid, case, event)})
    messages.append({"role": "user", "content": content})

    tools_since_reply, retried_guard = 0, False
    for _ in range(MAX_API_CALLS):
        try:
            resp = _call(client, messages)
        except anthropic.APIError as exc:
            store.log_event(conn, tid, "error", "api", None, {"error": str(exc)[:300]})
            return to_fallback("api_error")
        if resp.stop_reason == "refusal":
            return to_fallback("refusal")
        blocks = [_dump(b) for b in resp.content]
        messages.append({"role": "assistant", "content": blocks})
        if resp.stop_reason == "pause_turn":
            continue
        uses = [b for b in blocks if b.get("type") == "tool_use"]

        if not uses:
            text = _text_of(blocks)
            bad = guard.bad_numbers(text, guard.allowed_numbers(outputs))
            if bad and not retried_guard:
                retried_guard = True
                messages.append({"role": "user", "content": [{"type": "text", "text":
                    f"בתשובה יש מספרים שלא הופיעו בפלט של אף כלי: {', '.join(bad)}. "
                    "כתבי אותה שוב, רק עם מספרים שקיבלת מכלים, או בלי מספרים."}]})
                continue
            if bad:
                store.log_event(conn, tid, "error", "guard", {"text": text}, {"bad": bad})
                return to_fallback("guard")
            case["tool_outputs"] = outputs[-40:]
            return finish(conn, tid, case, messages, "reply", text, cards, did)

        results, asked = [], None
        for u in uses:
            name, inp = u["name"], u.get("input") or {}
            tools_since_reply += 1
            if name == "ask_teacher":
                if case.get("questions_asked", 0) >= tools.MAX_QUESTIONS or asked:
                    results.append({"type": "tool_result", "tool_use_id": u["id"], "is_error": True,
                                    "content": "אי אפשר לשאול עוד שאלה עכשיו (שאלה אחת בכל פעם, עד 3 בכל התהליך). המשיכי בלי."})
                    continue
                asked = u
                continue
            res, new_cards, d, is_err = tools.execute(ctx, name, inp)
            store.log_event(conn, tid, "tool", name, inp, res)
            outputs.append(res)
            cards += new_cards
            if d:
                did.append(d)
            results.append({"type": "tool_result", "tool_use_id": u["id"], "is_error": is_err,
                            "content": json.dumps(res, ensure_ascii=False, default=str)})

        if asked:
            inp = asked["input"]
            case["pending"] = {"id": asked["id"], "field": inp.get("field"), "school_year": inp.get("school_year"),
                               "question": inp.get("question"), "options": inp.get("options")}
            case["pending_results"] = results
            case["questions_asked"] = case.get("questions_asked", 0) + 1
            case["tool_outputs"] = outputs[-40:]
            qcard = tools.question_card(inp, ctx.get("status"))
            case["pending"]["card"] = qcard
            cards.append(qcard)
            text = _text_of(blocks)
            if guard.bad_numbers(text, guard.allowed_numbers(outputs)):
                text = ""
            store.log_event(conn, tid, "tool", "ask_teacher", inp, None)
            return finish(conn, tid, case, messages, "question", text, cards, did)

        messages.append({"role": "user", "content": results})
        if tools_since_reply >= MAX_TOOLS_WITHOUT_REPLY:
            # כלל 9: יותר מדי כלים בלי תשובה. מבקשים לסכם עכשיו, פעם אחת
            messages.append({"role": "user", "content": [{"type": "text", "text":
                "הפעלת הרבה כלים ברצף. עני עכשיו למורה במה שכבר יש לך, בלי כלים נוספים."}]})
            if tools_since_reply >= MAX_TOOLS_WITHOUT_REPLY + 3:
                break

    return to_fallback("stuck")
