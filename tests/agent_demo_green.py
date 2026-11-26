"""
המסלול הירוק מקצה לקצה, בלי מפתח API, על שרה כהן (T900):

    python tests/agent_demo_green.py

מאפס את שרה לנקודת ההתחלה של הדמו (agent/demo_docs.reset_demo), ומריץ:
תלוש ← הקורסים + עוז + קובץ זר ← "לא יודעת" ← חל"ת ושבתון ← תוצאה ← שתי פניות ← תזכורת.
בסוף מאפס אותה שוב, כך שהדמו מתחיל נקי.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("ANTHROPIC_API_KEY", None)

from db import get_connection  # noqa: E402

from agent import run_agent  # noqa: E402
from agent.demo_docs import make, reset_demo  # noqa: E402
from agent.store import save_upload  # noqa: E402

TID = "T900"
FAILS = []


def check(name, cond, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def up(*kinds):
    return {"type": "files", "doc_ids": [save_upload(TID, *make(k)) for k in kinds]}


def types(out, t):
    return [c for c in out["cards"] if c["type"] == t]


def main():
    conn = get_connection()
    reset_demo(conn, TID)
    conn.close()

    out = run_agent(TID, up("payslip"))
    check("תלוש נקרא", any(c["doc_type"] == "payslip" and c["ok"] for c in types(out, "file")), out)
    check("מבקש קובץ מהפורטל", bool(types(out, "guide")), out.get("text"))

    out = run_agent(TID, up("courses", "oz_join", "unknown"))
    files = {c["doc_type"]: c["ok"] for c in types(out, "file")}
    check("קורסים + עוז נקראו, הזר נדחה", files.get("courses") and files.get("oz_join") and files.get("unknown") is False, files)
    q = types(out, "question")
    check("שאלת שבתון", out["status"] == "question" and q and "לא יודעת" in q[0]["options"], out.get("text"))

    out = run_agent(TID, {"type": "answer", "value": "לא יודעת"})
    g = types(out, "guide")
    check("לא יודעת ← הדרכה לחל\"ת ושבתון", g and g[0]["doc_type"] == "vacations", out.get("text"))

    out = run_agent(TID, up("vacations"))
    r = types(out, "result")
    check("תוצאה: חסרות 4 יחידות", r and not r[0]["ok"] and abs(float(r[0]["gap_units"]) - 4) < 0.01, r)
    acts = types(out, "action")
    check("שתי פניות", {a["track"] for a in acts} == {"payroll", "institution"}, [a["track"] for a in acts])

    out = run_agent(TID, {"type": "sent", "track": "payroll"})
    check("נשלחה אחת, נשארה אחת", "נשארה" in (out.get("text") or ""), out.get("text"))
    out = run_agent(TID, {"type": "sent", "track": "institution"})
    check("שתיהן נשלחו ← תזכורת", bool(types(out, "reminder")), out.get("text"))

    conn = get_connection()
    reset_demo(conn, TID)
    conn.close()
    print("\n" + ("הכול עבר" if not FAILS else f"נכשלו {len(FAILS)}: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
