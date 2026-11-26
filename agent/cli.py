"""
שיחה עם הסוכן מהטרמינל, בלי המסך. לבדיקות מול Claude, וגיבוי לדמו.

    python -m agent.cli T900                      # שיחה עם שרה כהן
    python -m agent.cli T900 --reset              # מתחילים את השיחה מחדש (נתוני המורה נשארים)
    python -m agent.cli T900 --fallback           # רשת הביטחון בלבד, בלי AI
    python -m agent.cli T900 --files a.pdf b.jpg  # גרירת קבצים כבר בפתיחה

בתוך השיחה:
    טקסט חופשי        שאלה לסוכן
    1, 2, 3...        בחירת כפתור בשאלה פתוחה
    /files a.pdf b.pdf  גרירת קבצים
    /sent payroll     סימון שפנייה נשלחה
    /case             הצגת "התיק שלי"
    /reset            שיחה מחדש
    /quit             יציאה
"""
import argparse
import os
import sys
import time

from agent import run_agent
from agent.store import reset_case, save_upload
from db import get_connection

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code, s):
    return f"\033[{code}m{s}\033[0m" if USE_COLOR else s


def dim(s):
    return _c("2", s)


def bold(s):
    return _c("1", s)


def pink(s):
    return _c("35", s)


def green(s):
    return _c("32", s)


def red(s):
    return _c("31", s)


def n(x):
    """4.0 -> 4, 26.75 -> 26.75"""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return x
    return int(f) if f == int(f) else round(f, 2)


def show_card(c):
    t = c.get("type")
    if t == "file":
        mark = green("✓") if c.get("ok") else red("✕")
        print(f"  {mark} {bold(c.get('label', ''))}  {dim(c.get('detail', ''))}")
    elif t == "guide":
        print(f"  📍 {bold('איך מביאים: ' + c.get('label', ''))}")
        if c.get("why"):
            print(f"     {dim(c['why'])}")
        for i, s in enumerate(c.get("steps") or [], 1):
            print(f"     {i}. {s}")
        if not c.get("steps") and c.get("portal_path"):
            print(f"     {c['portal_path']}")
    elif t == "question":
        print(f"  ❓ {bold(c.get('text', ''))}")
        for i, o in enumerate(c.get("options") or [], 1):
            print(f"     {i}. {o}")
        if c.get("why"):
            print(f"     {dim(c['why'])}")
    elif t == "result":
        if c.get("ok"):
            print(f"  {green('✓ התלוש תקין')}: מגיע {n(c.get('expected'))}, בתלוש {n(c.get('paid'))}")
        else:
            ils = f" · כ-{round(c['gap_ils'])} ₪ בחודש" if c.get("gap_ils") else ""
            print(f"  {pink(bold('חסרות ' + str(n(c.get('gap_units'))) + ' יח׳'))}{ils}  "
                  f"{dim('מגיע ' + str(n(c.get('expected'))) + ', בתלוש ' + str(n(c.get('paid'))))}")
            for p in c.get("parts") or []:
                src = f" · {p['source']}" if p.get("source") else ""
                print(f"     +{n(p.get('units'))}  {p.get('label')}{dim(src)}")
                if p.get("quote"):
                    print(f"        {dim('«' + p['quote'][:160] + '»')}")
    elif t == "action":
        print(f"  ✉ {bold(c.get('title', ''))} → {c.get('to', '')}  {dim(c.get('channel') or '')}")
        if c.get("subject"):
            print(f"     נושא: {c['subject']}")
        if c.get("body"):
            for line in c["body"].splitlines()[:6]:
                print(f"     {dim(line)}")
            if len(c["body"].splitlines()) > 6:
                print(f"     {dim('…')}")


def show(out, took):
    if out.get("did"):
        print(dim("  " + "  ←  ".join("✓ " + d for d in out["did"])))
    tag = dim(" [רשת ביטחון]") if out.get("fallback") else ""
    if out.get("text"):
        print(f"{pink('סוכן')}{tag}: {out['text']}")
    for c in out.get("cards") or []:
        show_card(c)
    print(dim(f"  ({took:.1f} שנ', {out.get('status')})"))


def show_case(case):
    print(bold("התיק שלי") + dim(f"  {case.get('progress', 0)}%"))
    for d in case.get("docs") or []:
        mark = green("✓") if d["status"] == "read" else dim("·")
        print(f"  {mark} {d['label']}  {dim(d.get('detail') or d['status'])}")
    for f in case.get("facts") or []:
        src = {"manual": "הוזן ידנית", "portal": "מהפורטל", "payslip": "מהתלוש", "office": "מהמשרד"}.get(f["source"], f["source"])
        print(f"  • {f['label']}: {f.get('value')}  {dim(src)}")
    if case.get("result"):
        r = case["result"]
        print(f"  פער: {n(r.get('gap_units'))} יח'" + (f", כ-{round(r['gap_ils'])} ₪" if r.get("gap_ils") else ""))


def call(tid, event):
    t0 = time.time()
    out = run_agent(tid, event)
    show(out, time.time() - t0)
    return out


def upload(tid, paths):
    ids = []
    for p in paths:
        if not os.path.exists(p):
            print(red(f"  הקובץ לא נמצא: {p}"))
            continue
        with open(p, "rb") as f:
            ids.append(save_upload(tid, os.path.basename(p), f.read()))
    return ids


def main(argv=None):
    ap = argparse.ArgumentParser(description="שיחה עם סוכן OptiPay מהטרמינל")
    ap.add_argument("teacher_id")
    ap.add_argument("--reset", action="store_true", help="שיחה מחדש")
    ap.add_argument("--fallback", action="store_true", help="רשת הביטחון בלבד, בלי AI")
    ap.add_argument("--files", nargs="*", default=[], help="קבצים לגרירה בפתיחה")
    a = ap.parse_args(argv)

    if a.fallback:
        os.environ.pop("ANTHROPIC_API_KEY", None)
    conn = get_connection()
    t = conn.execute("SELECT full_name FROM teachers WHERE teacher_id=%s", (a.teacher_id,)).fetchone()
    if not t:
        print(red(f"אין מורה {a.teacher_id} במסד."))
        return 1
    if a.reset:
        reset_case(conn, a.teacher_id)
    conn.close()

    mode = "רשת ביטחון, בלי AI" if not os.environ.get("ANTHROPIC_API_KEY") else "Claude"
    print(bold(f"OptiPay · {t['full_name']} ({a.teacher_id})") + dim(f"  · {mode} · /quit ליציאה"))

    out = call(a.teacher_id, {"type": "files", "doc_ids": upload(a.teacher_id, a.files)} if a.files else {"type": "resume"})
    while True:
        try:
            line = input(bold("את: ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/quit", "/q", "/exit"):
            break
        if line == "/case":
            show_case(out.get("case") or {})
            continue
        if line == "/reset":
            conn = get_connection()
            reset_case(conn, a.teacher_id)
            conn.close()
            out = call(a.teacher_id, {"type": "resume"})
            continue
        if line.startswith("/files"):
            ids = upload(a.teacher_id, line.split()[1:])
            if ids:
                out = call(a.teacher_id, {"type": "files", "doc_ids": ids})
            continue
        if line.startswith("/sent"):
            out = call(a.teacher_id, {"type": "sent", "track": (line.split()[1:] or ["payroll"])[0]})
            continue
        q = next((c for c in out.get("cards") or [] if c.get("type") == "question"), None)
        if q and line.isdigit() and 1 <= int(line) <= len(q.get("options") or []):
            out = call(a.teacher_id, {"type": "answer", "value": q["options"][int(line) - 1]})
        elif q and out.get("status") == "question":
            out = call(a.teacher_id, {"type": "answer", "value": line})
        else:
            out = call(a.teacher_id, {"type": "message", "text": line})
    return 0


if __name__ == "__main__":
    sys.exit(main())
