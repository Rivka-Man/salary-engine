"""
בדיקת מסלול הדמו לפי .claude/skills/optipay-demo (סעיף 4), דרך האפליקציה עצמה (app.test_client):
reset ← תלוש ← הצטרפות לעוז ← הקורסים שלי ← "לא יודעת" ← "כן, שבתון" ← תוצאה ← שתי פניות ← תזכורת.

    set DATABASE_URL=local        (מומלץ: המסד המקומי של הדמו)
    python tests/demo_flow.py

מאפס את T900 בהתחלה ובסוף.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("ANTHROPIC_API_KEY", None)

from app import app  # noqa: E402
from db import get_connection  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def cards(out, t):
    return [c for c in out["cards"] if c["type"] == t]


def main():
    conn = get_connection()
    token = conn.execute("SELECT access_token FROM teachers WHERE teacher_id='T900'").fetchone()["access_token"]
    conn.close()
    c = app.test_client()
    base = f"/my/{token}/chat"
    post = lambda path, body: c.post(base + path, json=body).get_json()  # noqa: E731

    check("המסך נטען (200)", c.get(base).status_code == 200)
    post("/reset", {})

    o = post("/demo-file", {"kinds": ["payslip"]})
    f = cards(o, "file")
    check("1. תלוש · ספטמבר 2026 · שורה 249: 22.75", f and f[0]["ok"] and "ספטמבר 2026" in f[0]["detail"] and "22.75" in f[0]["detail"], f)
    g = cards(o, "guide")
    check("   עכשיו: הצטרפות לעוז לתמורה", g and g[0]["doc_type"] == "oz_join", g)

    o = post("/demo-file", {"kinds": ["oz_join"]})
    f = cards(o, "file")
    check("2. משרה ראשונה בעוז: 1.9.2011", f and "1.9.2011" in f[0]["detail"], f)
    g = cards(o, "guide")
    check("   עכשיו: הקורסים שלי, בלי תוצאה", g and g[0]["doc_type"] == "courses" and not cards(o, "result"), o["cards"])

    o = post("/demo-file", {"kinds": ["courses"]})
    f = cards(o, "file")
    check("3. 5 קורסים · 2021–2025", f and f[0]["detail"].startswith("5 קורסים") and "2021" in f[0]["detail"] and "2025" in f[0]["detail"], f)
    q = cards(o, "question")
    check('4. שאלה: שבתון או חל"ת בתשפ"ה', o["status"] == "question" and q and 'תשפ"ה' in q[0]["text"], q)

    o = post("/event", {"type": "answer", "value": "לא יודעת"})
    check('   "לא יודעת" ← הדרכה ואותה שאלה שוב', o["status"] == "question" and cards(o, "guide") and cards(o, "question")
          and not cards(o, "result"), [x["type"] for x in o["cards"]])

    o = post("/event", {"type": "answer", "value": "כן, שבתון"})
    r = cards(o, "result")
    ok = r and float(r[0]["expected"]) == 26.75 and float(r[0]["paid"]) == 22.75 and float(r[0]["gap_units"]) == 4
    check("5. מגיע 26.75 · בתלוש 22.75 · חסרות 4", ok, r)
    parts = sorted(float(p["units"]) for p in (r[0]["parts"] if r else []))
    check("   3 (תשפ\"ה) + 1 (תשפ\"ו), כ-97 ₪", parts == [1.0, 3.0] and r and round(r[0]["gap_ils"]) == 97, (parts, r and r[0]["gap_ils"]))
    check("6. פניות: חשבות השכר + מוסד ההשתלמות", {a["track"] for a in cards(o, "action")} == {"payroll", "institution"})

    o = post("/event", {"type": "sent", "track": "payroll"})
    o = post("/event", {"type": "sent", "track": "institution"})
    check("   אחרי שתיהן: תזכורת", bool(cards(o, "reminder")))

    o = post("/event", {"type": "message", "text": "כמה מס הכנסה אני משלמת?"})
    check("בוט: מחוץ לתחום", "מחוץ למה שאני בודקת" in o["text"], o["text"])
    o = post("/event", {"type": "message", "text": "מה דין קורס בחו\"ל בשנת השבתון?"})
    check("בוט: בלי מקור ← לא יודעת", o["text"].startswith("אני לא יודעת") or "(" in o["text"], o["text"])

    post("/reset", {})
    print("\n" + ("הכול עבר" if not FAILS else f"נכשלו {len(FAILS)}: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
