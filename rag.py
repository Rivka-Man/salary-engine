"""
מאגר הסעיפים לציטוט: sources/oz_sections.json.

get_rule_source(rule_id) - שליפה לפי הפניה (בלי חיפוש), ולכן מדויקת: כל חוק מקושר ידנית
לסעיף שלו, כי ה-source_ref בקוד לא כתוב בצורה אחידה.
search_sources(query) - חיפוש מילים פשוט בנוסח ובכותרות, לשאלות חופשיות.

כל עוד נוסח הסעיף ריק (רבקה ממלאת), מוחזר רק שם המסמך והסעיף, ו-quote הוא None.
"""
import json
import os
import re
from functools import lru_cache

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sources", "oz_sections.json")


@lru_cache(maxsize=1)
def _sections():
    with open(PATH, encoding="utf-8") as f:
        return json.load(f)["sections"]


def reload():
    _sections.cache_clear()


def label(sec):
    """'הסכם עוז לתמורה, סעיף 18(ה)'"""
    doc = (sec.get("source_doc") or "").replace("_", " ")
    if not sec.get("section"):
        return doc
    prefix = "סעיף " if doc.startswith("הסכם") or doc.startswith("נוהל") else ""
    return f"{doc}, {prefix}{sec['section']}"


def _out(sec):
    text = (sec.get("text") or "").strip()
    return {"source": label(sec), "title": sec.get("title") or "", "quote": text or None}


def get_rule_source(rule_id):
    """הסעיף/ים שהחוק מבוסס עליהם. [] אם החוק לא מקושר."""
    return [_out(s) for s in _sections() if rule_id in (s.get("rules") or [])]


def first_source(rule_id):
    """הסעיף הראשון של החוק, או None."""
    hits = get_rule_source(rule_id)
    return hits[0] if hits else None


def _words(s):
    return {w for w in re.findall(r"[\w\"׳״']+", s or "") if len(w) > 1}


def search_sources(query, k=3):
    """חיפוש פשוט לפי מילים משותפות בכותרת ובנוסח. מחזיר עד k סעיפים, הכי רלוונטי ראשון."""
    q = _words(query)
    if not q:
        return []
    scored = []
    for s in _sections():
        hay = _words(s.get("title")) | _words(s.get("text"))
        score = len(q & hay)
        if score:
            scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    return [dict(_out(s), rules=s.get("rules") or []) for _, s in scored[:k]]
