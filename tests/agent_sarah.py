"""
תסריט בדיקה לסוכן, על עותק זמני של שרה כהן (T900) בלי שנת השבתון.

    python tests/agent_sarah.py

לא נוגע בשרה עצמה. יוצר מורה זמנית M1-TEST, מריץ עליה:
  1. רשת הביטחון (בלי AI): כניסה ← שאלת שבתון ← "כן, שבתון" ← תוצאה + פניות
  2. הלולאה עם Claude מדומה (בלי מפתח API): אותו מסלול, דרך tool_use
  3. זיהוי קבצים: תמונה ← תלוש, PDF זר ← "לא שייך"
ובסוף מוחק את M1-TEST ואת כל מה שנוצר לה.
"""
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import payslips  # noqa: E402
from db import get_connection  # noqa: E402
from engine import init_db  # noqa: E402

from agent import run_agent  # noqa: E402
from agent.files import detect  # noqa: E402
from agent.store import reset_case  # noqa: E402

SRC, TID = "T900", "M1-TEST"
FAILS = []


def check(name, cond, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def clone():
    conn = get_connection()
    cleanup(conn)
    t = dict(conn.execute("SELECT * FROM teachers WHERE teacher_id=%s", (SRC,)).fetchone())
    t.update(teacher_id=TID, full_name="מורת בדיקה", access_token=secrets.token_urlsafe(16))
    cols = list(t)
    conn.execute(f"INSERT INTO teachers ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                 tuple(t[c] for c in cols))
    for c in conn.execute("SELECT * FROM courses WHERE teacher_id=%s", (SRC,)).fetchall():
        c = dict(c); c.pop("course_id"); c["teacher_id"] = TID
        ks = list(c)
        conn.execute(f"INSERT INTO courses ({', '.join(ks)}) VALUES ({', '.join(['%s'] * len(ks))})", tuple(c[k] for k in ks))
    s = dict(conn.execute("SELECT * FROM payslips WHERE teacher_id=%s ORDER BY payslip_id DESC LIMIT 1", (SRC,)).fetchone())
    s.pop("payslip_id"); s.update(teacher_id=TID, run_id=None, doc_id=None)
    ks = list(s)
    conn.execute(f"INSERT INTO payslips ({', '.join(ks)}) VALUES ({', '.join(['%s'] * len(ks))})", tuple(s[k] for k in ks))
    # בכוונה בלי employment_status_by_year: הסוכן צריך לשאול על השבתון
    conn.commit()
    conn.close()


def cleanup(conn=None):
    own = conn is None
    conn = conn or get_connection()
    for tbl in ("agent_events", "agent_cases", "payslips", "calculation_runs", "employment_status_by_year",
                "courses", "teacher_documents"):
        conn.execute(f"DELETE FROM {tbl} WHERE teacher_id=%s", (TID,))
    conn.execute("DELETE FROM teachers WHERE teacher_id=%s", (TID,))
    conn.commit()
    if own:
        conn.close()


def reset_status():
    conn = get_connection()
    conn.execute("DELETE FROM employment_status_by_year WHERE teacher_id=%s", (TID,))
    conn.execute("UPDATE payslips SET run_id=NULL WHERE teacher_id=%s", (TID,))
    reset_case(conn, TID)
    conn.close()


def cards(out, kind):
    return [c for c in out["cards"] if c["type"] == kind]


# ------------------------------------------------------------------

def test_fallback():
    print("\n1. רשת הביטחון, בלי AI")
    saved_key = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        out = run_agent(TID, {"type": "resume"})
        check("שואלת על שבתון", out["status"] == "question" and cards(out, "question"), out["status"])
        q = (cards(out, "question") or [{}])[0]
        check("השאלה על תשפ\"ה", q.get("school_year") == 'תשפ"ה', q.get("school_year"))
        check("fallback=True", out["fallback"] is True)
        again = run_agent(TID, {"type": "resume"})
        check("חזרה לקישור מציגה את אותה שאלה", cards(again, "question") and again["status"] == "question")
        out = run_agent(TID, {"type": "answer", "value": "כן, שבתון"})
        r = (cards(out, "result") or [{}])[0]
        check("כרטיס תוצאה", bool(r), out["status"])
        check("פער 4 יחידות", r.get("gap_units") == 4, r.get("gap_units"))
        check("מגיע 26.75, בתלוש 22.75", (r.get("expected"), r.get("paid")) == (26.75, 22.75), (r.get("expected"), r.get("paid")))
        check("כ-97 ₪", r.get("gap_ils") and abs(r["gap_ils"] - 97.08) < 0.05, r.get("gap_ils"))
        acts = cards(out, "action")
        check("שתי פניות: חשבות + מוסד", {a["track"] for a in acts} == {"payroll", "institution"}, [a["track"] for a in acts])
        check("ת\"ז לא בנוסח", all("להשלים" in a["body"] for a in acts if a["body"]))
        check("שבתון בתיק כ'ידני'", any(f["key"] == 'sabbatical:תשפ"ה' and f["source"] == "manual" for f in out["case"]["facts"]))
        check("did מסוכם", any("שמרתי" in d for d in out["did"]) and any("פניות" in d for d in out["did"]), out["did"])
        print("    טקסט:", out["text"])
    finally:
        if saved_key:
            os.environ["ANTHROPIC_API_KEY"] = saved_key


def test_dont_know():
    print("\n1ב. \"לא יודעת\" ← בקשת קובץ החופשות")
    reset_status()
    saved_key = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        out = run_agent(TID, {"type": "resume"})
        q = (cards(out, "question") or [{}])[0]
        check("יש כפתור 'לא יודעת'", "לא יודעת" in (q.get("options") or []), q.get("options"))
        out = run_agent(TID, {"type": "answer", "value": "לא יודעת"})
        g = (cards(out, "guide") or [{}])[0]
        check("כרטיס הדרכה לקובץ החופשות", g.get("doc_type") == "vacations", [c["type"] for c in out["cards"]])
        check("בלי כרטיס תוצאה (שלא יטעה)", not cards(out, "result"))
        check("הטקסט אומר איזה עמוד בפורטל", "ושבתון" in out["text"], out["text"])
        check("כרטיס ההדרכה עם צעדים מפורטים", len(g.get("steps") or []) >= 5, len(g.get("steps") or []))
        again = run_agent(TID, {"type": "resume"})
        check("חזרה לקישור: שוב מבקש את הקובץ, לא שואל שוב", cards(again, "guide") and not cards(again, "question"),
              [c["type"] for c in again["cards"]])
        print("    טקסט:", out["text"])
    finally:
        if saved_key:
            os.environ["ANTHROPIC_API_KEY"] = saved_key


class FakeResp:
    def __init__(self, content, stop="tool_use"):
        self.content, self.stop_reason = content, stop


class FakeClient:
    """Claude מדומה: מריץ תסריט קבוע של tool_use, כדי לבדוק את הלולאה בלי מפתח."""

    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.beta = self
        self.messages = self

    def create(self, **kw):
        import copy
        kw = copy.deepcopy(kw)  # הלולאה ממשיכה לשנות את רשימת ההודעות אחרי הקריאה
        self.calls.append(kw)
        step = self.script.pop(0)
        return step(kw) if callable(step) else step


def tu(i, name, inp):
    return {"type": "tool_use", "id": f"tu_{i}", "name": name, "input": inp}


def test_loop():
    print("\n2. הלולאה, עם Claude מדומה")
    reset_status()
    turn1 = FakeClient([
        FakeResp([{"type": "text", "text": "בודקת את התיק."}, tu(1, "case_status", {})]),
        FakeResp([tu(2, "ask_teacher", {"field": "sabbatical", "school_year": 'תשפ"ה',
                                        "question": 'היית בשבתון או בחל"ת בתשפ"ה?',
                                        "options": ["כן, שבתון", 'כן, חל"ת', "לא"]})]),
    ])
    out = run_agent(TID, {"type": "resume"}, client=turn1)
    check("עוצרת בשאלה", out["status"] == "question" and cards(out, "question"), out["status"])
    check("fallback=False", out["fallback"] is False)
    check("שתי קריאות ל-API", len(turn1.calls) == 2, len(turn1.calls))

    def final_text(kw):
        last = kw["messages"][-1]["content"]
        assert any(b.get("type") == "tool_result" for b in last), "חסר tool_result"
        return FakeResp([{"type": "text", "text": "בתלוש חסרות לך 4 יחידות, כ-97 ₪ בחודש. הכנתי 2 פניות. כדאי לשלוח את שתיהן עכשיו, במקביל."}], "end_turn")

    turn2 = FakeClient([
        lambda kw: FakeResp([tu(3, "run_check", {"what_if": None})]) if kw["messages"][-1]["content"][-1]["tool_use_id"] == "tu_2" else None,
        FakeResp([tu(4, "run_check", {"what_if": {"year_status": [{"school_year": 'תשפ"ה', "status": "רגיל"}],
                                                   "drop_course_ids": None, "teacher_flags": None}})]),
        FakeResp([tu(5, "plan_actions", {})]),
        final_text,
    ])
    out = run_agent(TID, {"type": "answer", "value": "כן, שבתון"}, client=turn2)
    check("תשובה", out["status"] == "reply", out["status"])
    check("כרטיס תוצאה עם פער 4", (cards(out, "result") or [{}])[0].get("gap_units") == 4)
    check("שתי פניות", len(cards(out, "action")) == 2, len(cards(out, "action")))
    check("did כולל 'מה אם'", "בדקתי מה אם" in out["did"], out["did"])
    wf = [kw for kw in turn2.calls][2]["messages"][-1]["content"][0]["content"]
    check("'מה אם' לא נשמר כתוצאה", '"what_if": true' in wf)

    print("\n   בדיקת המספרים: מודל שממציא מספר")
    reset_status()
    run_agent(TID, {"type": "answer", "value": "x"}, client=FakeClient([FakeResp([{"type": "text", "text": "שלום"}], "end_turn")]))
    liar = FakeClient([
        FakeResp([tu(6, "run_check", {"what_if": None})]),
        FakeResp([{"type": "text", "text": "חסרות לך 7 יחידות, 250 ₪ בחודש."}], "end_turn"),
        FakeResp([{"type": "text", "text": "חסרות לך 7 יחידות, 250 ₪ בחודש."}], "end_turn"),
    ])
    out = run_agent(TID, {"type": "message", "text": "כמה חסר?"}, client=liar)
    check("ניסיון תיקון אחד, ואז רשת ביטחון", out["fallback"] is True and len(liar.calls) == 3, (out["fallback"], len(liar.calls)))
    check("הטקסט הסופי בלי 250", "250" not in out["text"], out["text"])


def test_detect():
    print("\n3. זיהוי קבצים (בלי DB)")
    fake_slip = lambda f, d: {"is_payslip": True, "pay_month": "2026-09", "gimul": {"units": 22.75}}

    def not_slip(f, d):
        raise payslips.PayslipError("הקובץ לא נראה כמו תלוש שכר.")

    def no_key(f, d):
        raise payslips.PayslipError("קריאה אוטומטית של התלוש לא זמינה כרגע (לא הוגדר ANTHROPIC_API_KEY).")

    check("צילום ← תלוש", detect("slip.jpg", b"\xff\xd8\xff", fake_slip)[0] == "payslip")
    check("PDF זר ← לא שייך", detect("award.pdf", b"%PDF-1.4 x", not_slip)[0] == "unknown")
    check("בלי מפתח ← תלוש שלא נקרא", detect("slip.pdf", b"%PDF-1.4 x", no_key)[0] == "payslip_failed")
    check("קובץ Word ← לא שייך", detect("notes.docx", b"PK..", fake_slip)[0] == "unknown")


if __name__ == "__main__":
    init_db(reset=False)  # יוצר את טבלאות הסוכן אם חסרות (IF NOT EXISTS)
    clone()
    try:
        test_detect()
        test_fallback()
        test_dont_know()
        test_loop()
    finally:
        cleanup()
    print("\n" + ("הכול עבר." if not FAILS else f"{len(FAILS)} נכשלו: " + ", ".join(FAILS)))
    sys.exit(1 if FAILS else 0)
