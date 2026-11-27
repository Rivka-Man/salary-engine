"""
הכלים של הסוכן. כל כלי עוטף קוד שכבר קיים, ומחזיר:
  result - מה ש-Claude מקבל (קצר, בלי פרטים אישיים)
  cards  - כרטיסים למסך, נבנים בקוד ולא ע"י המודל (חוזה: agent/contracts.py)
  did    - שורה קצרה ל"מה עשיתי"

ask_teacher לא רץ כאן עד הסוף: הוא עוצר את הלולאה (ראו loop.py), והתשובה נשמרת
ע"י save_answer כשהמורה עונה.
"""
import re
import json
import os

import ask
import payslips
import playbook
import rag
from engine import compute_gimul_hishtalmut, get_courses, get_status_by_year, get_teacher

from agent import case as case_mod
from agent import send
from agent.files import read_file as _read_file
from agent.store import DOC_LABELS, portal_guide as _portal_guide, set_fact

MAX_QUESTIONS = 3


def _obj(props, required=None):
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


_STR = {"type": "string"}
_NSTR = {"anyOf": [{"type": "string"}, {"type": "null"}]}

TOOL_DEFS = [
    {"name": "read_file", "strict": True,
     "description": "מזהה איזה מסמך המורה העלתה (תלוש / הקורסים שלי / הצטרפות לעוז / מכתב העסקה / לא שייך), קורא אותו ושומר את הנתונים בתיק. קראי לכלי הזה לכל doc_id חדש.",
     "input_schema": _obj({"doc_id": {"type": "integer"}})},
    {"name": "case_status", "strict": True,
     "description": "מה ידוע על המורה ומאיזה מקור, מה חסר, ומה הצעד שיקדם את התשובה הכי הרבה. קראי אחרי קריאת קבצים ולפני שמחליטים מה לבקש.",
     "input_schema": _obj({})},
    {"name": "portal_guide", "strict": True,
     "description": "המסלול בפורטל עובדי הוראה וצילום מסך לקובץ שצריך להביא. מוצג למורה ככרטיס הדרכה.",
     "input_schema": _obj({"doc_type": {"type": "string", "enum": ["payslip", "courses", "oz_join", "maktav", "vacations", "gmulim"]}})},
    {"name": "ask_teacher", "strict": True,
     "description": "שאלה אחת למורה עם 2-4 כפתורים. רק לנתון שחסר, שמשנה את התשובה ושאי אפשר לקרוא מקובץ. השתמשי בשאלה ובאפשרויות שהחזיר case_status. עד 3 שאלות בכל התהליך. השיחה נעצרת עד שהמורה עונה.",
     "input_schema": _obj({"field": {"type": "string", "enum": ["sabbatical", "double_degree", "pensioner", "dual_reform"]},
                           "school_year": _NSTR,
                           "question": _STR,
                           "options": {"type": "array", "items": _STR}})},
    {"name": "run_check", "strict": True,
     "description": "מריץ את מנוע החוקים לחודש התלוש ומשווה לשורה 249. מחזיר כמה מגיע, כמה בתלוש, הפער ביחידות ובש\"ח, ופירוק לרכיבים. what_if: חישוב 'מה אם' שלא נשמר ולא מוצג למורה, כדי לבדוק מאיפה הפער. בלי what_if: החישוב האמיתי.",
     "input_schema": _obj({"what_if": {"anyOf": [{"type": "null"}, _obj({
         "year_status": {"anyOf": [{"type": "null"}, {"type": "array", "items": _obj({
             "school_year": _STR, "status": {"type": "string", "enum": ["רגיל", "שבתון", "חצי-שבתון", "חלת"]}})}]},
         "drop_course_ids": {"anyOf": [{"type": "null"}, {"type": "array", "items": {"type": "integer"}}]},
         "teacher_flags": {"anyOf": [{"type": "null"}, {"type": "array", "items": _obj({
             "field": {"type": "string", "enum": ["has_double_degree_gimul", "has_beinayim_gimul",
                                                  "has_additional_double_degree_gimul", "is_pensioner",
                                                  "dual_reform_ofek_hadash"]},
             "value": {"type": "integer", "enum": [0, 1]}})}]},
     })]}})},
    {"name": "plan_actions", "strict": True,
     "description": "אחרי run_check עם פער: בוחר למי פונים (סיבות A-E) ומכין נוסח לכל פנייה. הנוסחים מוצגים למורה ככרטיסים עם כפתור 'שלחי'.",
     "input_schema": _obj({})},
    {"name": "explain", "strict": True,
     "description": "העובדות להסבר: החוקים שהופעלו עם הסעיף בהסכם, ושאלות נפוצות עם תשובות מוכנות. לשאלה של המורה על התוצאה.",
     "input_schema": _obj({"rule_id": _NSTR})},
    {"name": "search_sources", "strict": True,
     "description": "חיפוש במאגר הסעיפים לשאלה חופשית. מחזיר סעיפים עם שם המסמך ומספר הסעיף.",
     "input_schema": _obj({"query": _STR})},
    {"name": "set_reminder", "strict": True,
     "description": "תזכורת לבדוק את התלוש הבא, אחרי שהמורה שלחה פניות.",
     "input_schema": _obj({"note": _STR})},
]

TOOL_NAMES = {t["name"] for t in TOOL_DEFS}


# ------------------------------------------------------------------
# עזרים
# ------------------------------------------------------------------

def _teacher_urls(conn, tid, payslip_id=None):
    t = conn.execute("SELECT access_token FROM teachers WHERE teacher_id=%s", (tid,)).fetchone()
    tok = t["access_token"] if t else ""
    return {
        "slip_url": f"/my/{tok}/payslip" + (f"?id={payslip_id}" if payslip_id else ""),
        "how_url": f"/my/{tok}/payslip?open=how" + (f"&id={payslip_id}" if payslip_id else ""),
        "todo_url": f"/my/{tok}/payslip/{payslip_id}/todo" if payslip_id else "",
    }


def _latest_slip(conn, tid):
    return conn.execute(
        "SELECT * FROM payslips WHERE teacher_id=%s ORDER BY pay_month DESC, payslip_id DESC LIMIT 1", (tid,)
    ).fetchone()


def _component_rule(c, status_by_year):
    key = c["key"]
    if key.startswith("year:"):
        st = status_by_year.get(c.get("school_year"), "רגיל")
        return "R12_SABBATICAL_EXEMPT" if st != "רגיל" else "R11_ANNUAL_CAP"
    return {"base": "R08C_PRE_REFORM_COMPUTED", "transition": "R08_TRANSITION_UNIT",
            "dd": "R04_DOUBLE_DEGREE", "cap": "R11_ANNUAL_CAP"}.get(key)


def _source(rule_id):
    s = rag.first_source(rule_id) if rule_id else None
    return (s["source"], s["quote"]) if s else ("", None)


def _summary(cmp, status_by_year):
    comps = []
    for c in cmp["components"]:
        rid = c.get("rule_id") or _component_rule(c, status_by_year)
        comps.append({"key": c["key"], "label": c["label"], "units": c["units"],
                      "missing_in_payslip": bool(c.get("flagged")), "rule_id": rid})
    return {
        "expected_units": cmp["expected_units"], "paid_units": cmp["paid_units"],
        "gap_units": cmp["gap_units"], "gap_ils_monthly": None if cmp["gap_ils"] is None else round(cmp["gap_ils"], 2),
        "unit_value_ils": None if cmp["unit_ils"] is None else round(cmp["unit_ils"], 2),
        "unit_pct_of_salary": cmp["rate"], "line_missing": cmp["line_missing"], "ok": cmp["ok"],
        "diagnosis": cmp["cause"]["title"], "components": comps,
    }


def _check(conn, tid, slip, overrides=None):
    calc_date = f"{slip['pay_month']}-01"
    result = compute_gimul_hishtalmut(tid, calc_date, overrides=overrides or None)
    run_id = None
    if not overrides:
        run_id = conn.execute("SELECT max(run_id) AS r FROM calculation_runs WHERE teacher_id=%s", (tid,)).fetchone()["r"]
        conn.execute("UPDATE payslips SET run_id=%s WHERE payslip_id=%s", (run_id, slip["payslip_id"]))
        conn.commit()
    return result, payslips.compare(conn, result, slip)


def _current_cmp(conn, tid):
    """ההשוואה האחרונה שנשמרה (אחרי run_check אמיתי)."""
    slip = _latest_slip(conn, tid)
    if not slip or not slip["run_id"]:
        return None, None, None
    run = conn.execute("SELECT result_json FROM calculation_runs WHERE run_id=%s", (slip["run_id"],)).fetchone()
    if not run:
        return None, None, None
    result = json.loads(run["result_json"])
    return slip, result, payslips.compare(conn, result, slip)


# ------------------------------------------------------------------
# הכלים
# ------------------------------------------------------------------

def t_read_file(ctx, doc_id, extract_payslip=None):
    r = _read_file(ctx["conn"], ctx["teacher_id"], doc_id, extract_payslip=extract_payslip)
    ctx["case"]["docs"][str(doc_id)] = {"doc_type": r["doc_type"], "ok": r["ok"], "detail": r["detail"]}
    card = {"type": "file", "doc_id": doc_id, "doc_type": r["doc_type"],
            "label": r["label"] if r["doc_type"] != "unknown" else r.get("filename", r["label"]),
            "detail": r["detail"], "ok": r["ok"]}
    res = {k: r[k] for k in ("ok", "doc_type", "label", "detail", "saved", "problems")}
    return res, [card], ("read_ok" if r["ok"] else ("read_unknown" if r["doc_type"] == "unknown" else "read_failed"))


def t_case_status(ctx):
    st = case_mod.case_status(ctx["conn"], ctx["teacher_id"], ctx["case"])
    ctx["status"] = st
    res = {"docs": [{k: d[k] for k in ("doc_type", "status", "detail")} for d in st["docs"]],
           "known": st["known"], "missing": st["missing"], "ready_to_check": st["ready_to_check"],
           "questions_left": st["questions_left"]}
    return res, [], "בדקתי מה חסר"


def t_portal_guide(ctx, doc_type):
    g = _portal_guide(doc_type)
    card = {"type": "guide", "doc_type": doc_type, "label": g.get("label") or DOC_LABELS.get(doc_type),
            "portal_path": g.get("portal_path"), "steps": g.get("steps") or [], "image": g.get("image"),
            "why": g.get("why")}
    # המודל מקבל רק את המסלול והסיבה. הצעדים המפורטים מוצגים בכרטיס, אין צורך לחזור עליהם בטקסט
    return ({"label": card["label"], "portal_path": card["portal_path"], "why": card["why"],
             "note": "הצעדים המפורטים מוצגים למורה בכרטיס. אל תחזרי עליהם בטקסט."},
            [card], f"הכנתי הדרכה: {card['label']}")


def _overrides(what_if):
    """מבנה ה-what_if של הכלי (רשימות, כי strict) ← המבנה שהמנוע מקבל."""
    w = what_if or {}
    out = {}
    if w.get("year_status"):
        out["year_status"] = {x["school_year"]: x["status"] for x in w["year_status"]}
    if w.get("drop_course_ids"):
        out["drop_course_ids"] = list(w["drop_course_ids"])
    if w.get("teacher_flags"):
        out["teacher"] = {x["field"]: x["value"] for x in w["teacher_flags"]}
    return out


def t_run_check(ctx, what_if=None):
    conn, tid = ctx["conn"], ctx["teacher_id"]
    slip = _latest_slip(conn, tid)
    if not slip:
        return {"error": "אין תלוש בתיק. צריך תלוש לפני הבדיקה."}, [], None
    overrides = _overrides(what_if)
    result, cmp = _check(conn, tid, slip, overrides)
    status_by_year = dict(get_status_by_year(conn, tid), **(overrides.get("year_status") or {}))
    summary = dict(_summary(cmp, status_by_year), pay_month=slip["pay_month"], what_if=bool(overrides))
    if overrides:
        return summary, [], "בדקתי מה אם"
    ctx["case"]["result"] = {"gap_units": cmp["gap_units"], "ok": cmp["ok"],
                             "gap_ils": None if cmp["gap_ils"] is None else round(cmp["gap_ils"], 2),
                             "expected": cmp["expected_units"], "paid": cmp["paid_units"]}
    cards = []
    if not cmp["ok"]:
        parts = []
        for c in summary["components"]:
            if not c["missing_in_payslip"]:
                continue
            src, quote = _source(c["rule_id"])
            parts.append({"units": c["units"], "label": c["label"], "rule_id": c["rule_id"], "source": src, "quote": quote})
        urls = _teacher_urls(conn, tid, slip["payslip_id"])
        cards.append({"type": "result", "ok": False, "expected": cmp["expected_units"], "paid": cmp["paid_units"],
                      "gap_units": cmp["gap_units"], "gap_ils": summary["gap_ils_monthly"],
                      "pay_month": slip["pay_month"], "parts": parts,
                      "slip_url": urls["slip_url"], "how_url": urls["how_url"]})
    else:
        cards.append({"type": "result", "ok": True, "expected": cmp["expected_units"], "paid": cmp["paid_units"],
                      "gap_units": 0, "gap_ils": 0, "pay_month": slip["pay_month"], "parts": [],
                      **{k: v for k, v in _teacher_urls(conn, tid, slip["payslip_id"]).items() if k != "todo_url"}})
    return summary, cards, "הרצתי את המנוע והשוויתי לתלוש"


def t_plan_actions(ctx):
    conn, tid = ctx["conn"], ctx["teacher_id"]
    slip, result, cmp = _current_cmp(conn, tid)
    if not cmp:
        return {"error": "קודם צריך להריץ run_check."}, [], None
    if cmp["ok"]:
        return {"ok": True, "tracks": []}, [], "אין צורך בפנייה"
    teacher = get_teacher(conn, tid)
    courses = get_courses(conn, tid)
    plan = playbook.plan(cmp, courses, teacher, slip)
    urls = _teacher_urls(conn, tid, slip["payslip_id"])
    cards, tracks = [], []
    for t in plan["tracks"]:
        tracks.append({"track": t["id"], "cause": t["cause"], "to": t.get("letter_to") or t["who"],
                       "title": t["title"], "has_letter": bool(t.get("letter"))})
        subject, body = t.get("letter_subject") or "", t.get("letter") or ""
        cards.append({"type": "action", "track": t["id"], "cause": t["cause"], "to": t.get("letter_to") or t["who"],
                      "title": t["title"], "channel": t.get("sub") or t.get("channel"), "link": t.get("link"),
                      "subject": subject, "body": body,
                      "attach": t.get("attach") or [], "steps": t.get("step_lines") or [],
                      "send": send.send_options(t["id"], subject, body, link=t.get("link")),
                      "sent": t["id"] in (ctx["case"].get("sent") or []),
                      "todo_url": urls["todo_url"]})
    ctx["case"]["plan"] = {"causes": plan["causes"], "tracks": [t["track"] for t in tracks]}
    # תזכורת לחודש הבא יוצאת יחד עם הפניות, כדי שהמורה תכניס אותה ליומן באותו רגע
    cards.append(send.reminder_card(_personal_link(conn, tid)))
    res = {"causes": plan["causes"], "parallel": plan["parallel"], "tracks": tracks,
           "lead": (plan.get("lead") or "").replace("**", ""),
           "note": "לכל פנייה יש כפתור שפותח את הפורטל או את המייל עם הנוסח, וכרטיס תזכורת ליומן. אל תחזרי על הנוסחים בטקסט."}
    n_letters = len([c for c in cards if c["type"] == "action"])
    return res, cards, f"הכנתי {n_letters} {'פניות' if n_letters != 1 else 'פנייה'}"


def t_explain(ctx, rule_id=None):
    conn, tid = ctx["conn"], ctx["teacher_id"]
    slip, result, cmp = _current_cmp(conn, tid)
    if not cmp:
        return {"error": "קודם צריך להריץ run_check."}, [], None
    rules = {r["rule_id"]: r for r in conn.execute(
        "SELECT rule_id, title, description, source_doc, source_ref FROM rules").fetchall()}
    status = get_status_by_year(conn, tid)
    f = ask.facts(cmp, result, rules)
    for rid, r in f["rules"].items():
        src, quote = _source(rid)
        r["source"] = src or r["source"]
        r["quote"] = quote
    if rule_id:
        f["rules"] = {k: v for k, v in f["rules"].items() if k == rule_id}
    f["saved_questions"] = [{"q": q["q"], "a": q["a"], "cite": q["cite"]}
                            for q in ask.saved_questions(cmp, result, rules, status)]
    f.pop("trace", None)  # ארוך; החוקים והרכיבים מספיקים להסבר
    return f, [], "שלפתי את הסעיף"


def t_search_sources(ctx, query):
    hits = rag.search_sources(query)
    return {"hits": hits}, [], "חיפשתי במאגר הסעיפים"


def t_set_reminder(ctx, note):
    card = send.reminder_card(_personal_link(ctx["conn"], ctx["teacher_id"]))
    ctx["case"]["reminder"] = {"note": note, "dates": [i["date"] for i in card["items"]]}
    return ({"ok": True, "dates": [{"label": i["label"], "date": i["date"]} for i in card["items"]]},
            [card], "הכנתי תזכורת ליומן")


def _personal_link(conn, tid):
    """הקישור האישי של המורה, לתוך התזכורת ביומן. PUBLIC_URL = כתובת האתר (למשל ב-Render)."""
    t = conn.execute("SELECT access_token FROM teachers WHERE teacher_id=%s", (tid,)).fetchone()
    if not t or not t["access_token"]:
        return ""
    return os.environ.get("PUBLIC_URL", "").rstrip("/") + f"/my/{t['access_token']}/chat"


def question_card(inp, status=None):
    why = ""
    for m in (status or {}).get("missing") or []:
        if m.get("field") == inp.get("field") and (m.get("school_year") == inp.get("school_year") or not inp.get("school_year")):
            why = m.get("why") or ""
            break
    return {"type": "question", "field": inp["field"], "school_year": inp.get("school_year"),
            "text": inp["question"], "options": inp["options"], "why": why}


# ------------------------------------------------------------------
# שמירת תשובה של המורה ("הוזן ידנית")
# ------------------------------------------------------------------

def _status_from_answer(value):
    v = value or ""
    if "חצי" in v:
        return "חצי-שבתון"
    if "חל" in v and "ת" in v and "שבתון" not in v:
        return "חלת"
    if "שבתון" in v:
        return "שבתון"
    return "רגיל"


# תשובה בטקסט חופשי (ולא בכפתור): "לא יודע", "לא בטוחה", "לא זוכרת", "אין לי מושג"
_DONT_KNOW = re.compile(r"יודע|בטוח|זוכר|מושג|אין לי מידע|\?")


def sabbatical_answer(value):
    """
    מתרגם תשובה לשאלת השבתון. מחזיר "dont_know", סטטוס (שבתון / חצי-שבתון / חלת / רגיל),
    או None כשלא ברור (למשל "כן" בלי לומר מה). None: שואלים שוב, לא מנחשים.
    """
    v = (value or "").strip()
    if not v or _DONT_KNOW.search(v):
        return "dont_know" if v else None
    if "חצי" in v:
        return "חצי-שבתון"
    if "שבתון" in v:
        return "שבתון"
    if re.search(r'חל"?ת|חופשה ללא', v):
        return "חלת"
    if re.fullmatch(r"(לא|ממש לא|לא היי?תי|רגיל[הה]?|שנה רגילה|לא,? .*רגיל.*)[.!]?", v):
        return "רגיל"
    return None


def save_answer(conn, tid, case, pending, value):
    """שומר את תשובת המורה לשאלה ממתינה. מחזיר מה נשמר, לתוצאת הכלי ask_teacher."""
    field, sy = pending.get("field"), pending.get("school_year")
    v = (value or "").strip()
    if field == "sabbatical" and sy and sabbatical_answer(v) is None:
        return {"saved": None, "answer": v, "unclear": True,
                "note": "התשובה לא ברורה. שאלי שוב, עם האפשרויות."}
    if field == "sabbatical" and sy and sabbatical_answer(v) == "dont_know":
        # לא שומרים סטטוס. מסמנים "לא ידוע", ו-case_status יבקש את קובץ החופשות
        set_fact(case, f"sabbatical:{sy}", f"שבתון {sy}", case_mod.DONT_KNOW, "manual")
        conn.commit()
        return {"saved": None, "answer": v, "need_file": "vacations", "school_year": sy,
                "note": f"המורה לא יודעת אם {sy} הייתה שנת שבתון. בקשי את קובץ החופשות והשבתונים (portal_guide vacations)."}
    if field == "sabbatical" and sy:
        st = sabbatical_answer(v)
        conn.execute(
            """INSERT INTO employment_status_by_year (teacher_id, school_year, status) VALUES (%s,%s,%s)
               ON CONFLICT (teacher_id, school_year) DO UPDATE SET status=EXCLUDED.status""", (tid, sy, st))
        names = {"שבתון": "שבתון", "חצי-שבתון": "חצי שבתון", "חלת": 'חל"ת', "רגיל": "שנה רגילה"}
        label = names.get(st, st) + " " + sy
        set_fact(case, f"sabbatical:{sy}", label, st, "manual")
        saved = {"school_year": sy, "status": st}
        conn.commit()
        return {"saved": saved, "label": label, "answer": v, "source": "manual"}
    elif field == "double_degree":
        dd = 1 if "כפל" in v else 0
        mid = 1 if "ביניים" in v else 0
        if "לא יודעת" in v:
            set_fact(case, "double_degree_unknown", "כפל תואר", "לא ידוע", "manual")
            return {"saved": None, "answer": v, "need_file": "gmulim",
                    "note": "המורה לא יודעת. בעמוד הגמולים בפורטל רואים אם מוכר לה כפל תואר (portal_guide gmulim)."}
        conn.execute("UPDATE teachers SET has_double_degree_gimul=%s, has_beinayim_gimul=%s WHERE teacher_id=%s",
                     (dd, mid, tid))
        set_fact(case, "double_degree", "כפל תואר / ביניים", "כפל תואר" if dd else ("ביניים" if mid else "לא"), "manual")
        saved = {"double_degree": bool(dd), "beinayim": bool(mid)}
    elif field in ("pensioner", "dual_reform"):
        col = "is_pensioner" if field == "pensioner" else "dual_reform_ofek_hadash"
        yes = 1 if v.startswith("כן") else 0
        conn.execute(f"UPDATE teachers SET {col}=%s WHERE teacher_id=%s", (yes, tid))
        set_fact(case, field, "גמלאית" if field == "pensioner" else "גם באופק חדש", "כן" if yes else "לא", "manual")
        saved = {field: bool(yes)}
    else:
        saved = None
    conn.commit()
    label = next((f["label"] + ": " + str(f["value"]) for f in case["facts"] if f["key"] == field), None)
    return {"saved": saved, "label": label, "answer": v, "source": "manual"}


# ------------------------------------------------------------------
# הרצה
# ------------------------------------------------------------------

HANDLERS = {
    "read_file": lambda ctx, i: t_read_file(ctx, i["doc_id"], ctx.get("extract_payslip")),
    "case_status": lambda ctx, i: t_case_status(ctx),
    "portal_guide": lambda ctx, i: t_portal_guide(ctx, i["doc_type"]),
    "run_check": lambda ctx, i: t_run_check(ctx, i.get("what_if")),
    "plan_actions": lambda ctx, i: t_plan_actions(ctx),
    "explain": lambda ctx, i: t_explain(ctx, i.get("rule_id")),
    "search_sources": lambda ctx, i: t_search_sources(ctx, i["query"]),
    "set_reminder": lambda ctx, i: t_set_reminder(ctx, i["note"]),
}


def execute(ctx, name, inp):
    """מריץ כלי (חוץ מ-ask_teacher). מחזיר (result, cards, did, is_error)."""
    fn = HANDLERS.get(name)
    if not fn:
        return {"error": f"כלי לא מוכר: {name}"}, [], None, True
    try:
        res, cards, did = fn(ctx, inp or {})
        return res, cards, did, bool(isinstance(res, dict) and res.get("error"))
    except Exception as exc:
        try:
            ctx["conn"].rollback()
        except Exception:
            pass
        return {"error": f"{exc.__class__.__name__}: {exc}"[:300]}, [], None, True
