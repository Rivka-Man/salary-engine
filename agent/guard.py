"""
בדיקת הפלט של הסוכן (כלל 1 באפיון: "לא מחשב").

כל מספר בטקסט שהסוכן כותב למורה חייב להופיע בפלט של כלי שרץ בתיק הזה, אחרי
עיגול סביר. מספר שלא מופיע = המודל חישב או המציא, והתשובה לא מוצגת.
"""
import json
import re

# מספרים שתמיד מותרים: ספירות קטנות ("שתי פניות", "3 קבצים"), המוקד, תקרת השאלות
ALWAYS_OK = {str(n) for n in range(0, 11)} | {"6552"}

_NUM = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def _norm(s):
    """'1,867.10' -> '1867.1', '97.00' -> '97', '09' -> '9'"""
    s = s.replace(",", "")
    try:
        f = float(s)
    except ValueError:
        return s
    if f == int(f):
        return str(int(f))
    return f"{f:.4f}".rstrip("0").rstrip(".")


def _variants(x):
    """צורות מקובלות של מספר מפלט כלי: עצמו, מעוגל לשלם, ל-1 ול-2 ספרות."""
    out = set()
    try:
        f = float(x)
    except (TypeError, ValueError):
        return out
    out.add(_norm(str(f)))
    for d in (0, 1, 2):
        out.add(_norm(f"{round(f, d):.{d}f}"))
    return out


def allowed_numbers(tool_outputs):
    """כל המספרים שמותר להזכיר: מכל פלט של כלי (כולל מספרים בתוך מחרוזות, כמו תאריכים)."""
    ok = set(ALWAYS_OK)

    def walk(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            ok.update(_variants(v))
        elif isinstance(v, str):
            for m in _NUM.findall(v):
                ok.update(_variants(m.replace(",", "")))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    for o in tool_outputs:
        walk(o if not isinstance(o, str) else _try_json(o))
    return ok


def _try_json(s):
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return s


def bad_numbers(text, allowed):
    """המספרים בטקסט שלא הופיעו בשום פלט של כלי."""
    bad = []
    for m in _NUM.findall(text or ""):
        n = _norm(m)
        if n not in allowed:
            bad.append(m)
    return bad
