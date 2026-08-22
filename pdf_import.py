"""
ייבוא קורסים מקובץ PDF שהורד מפורטל עו"ה (עמוד "הקורסים שלי").
הפורטל לא מייצא PDF עם טבלה אמיתית (רק טקסט בפריסת הדפסה), והעברית
בתוכו שמורה בכיוון הפוך תו-אחר-תו - לכן הפענוח מבוסס על מיקום מילים
בעמוד (לא על extract_table הרגיל של pdfplumber, שמפיק תאים מבולגנים
על הקובץ הזה).

מה שכן ניתן לשלוף באמינות: תאריך תחילה/סיום, שעות, וסטטוס ההכרה
כפי שמופיע בפורטל (עוז לתמורה / לא ברפורמה). שם הקורס והמוסד נשמרים
כטקסט חופשי ("label") לצורך תצוגה בלבד בתצוגה המקדימה - הסכמה של
courses לא כוללת שדה שם/מוסד ממילא.
"""
import re

import pdfplumber

HEBREW_RE = re.compile(r"[֐-׿]")
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
HEADER_MARKERS = ("שם וסמל", "היקף", "משתתף", "תאריך", "מוסד", "הקורסים", "נתונים", "פיתוח")


def _fix_word(w):
    return w[::-1] if HEBREW_RE.search(w) else w


def _lines_for_page(page):
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    buckets = {}
    for w in words:
        key = round(w["top"] / 3) * 3
        buckets.setdefault(key, []).append(w)
    lines = []
    for top in sorted(buckets.keys()):
        row_words = sorted(buckets[top], key=lambda w: -w["x0"])
        tokens = [_fix_word(w["text"]) for w in row_words if w["text"] != "!"]
        lines.append(tokens)
    return lines


def _to_iso_date(ddmmyyyy):
    d, m, y = ddmmyyyy.split("/")
    return f"{y}-{m}-{d}"


def parse_courses_pdf(file_path_or_buffer):
    """
    מחזיר רשימת dict-ים, אחד לכל קורס שזוהה:
    {label, start_date (ISO), end_date (ISO, לתצוגה בלבד), hours,
     source_type_guess, pre_approved_guess}
    """
    rows = []
    pending_label = []
    seen_header = False

    with pdfplumber.open(file_path_or_buffer) as pdf:
        for page in pdf.pages:
            for tokens in _lines_for_page(page):
                joined = " ".join(tokens)
                dates = [t for t in tokens if DATE_RE.match(t)]

                if len(dates) != 2 and any(m in joined for m in HEADER_MARKERS):
                    seen_header = True
                    continue

                if len(dates) == 2:
                    idx1 = tokens.index(dates[0])
                    idx2 = tokens.index(dates[1])
                    before = tokens[:idx1]
                    after = tokens[idx2 + 1:]

                    # שני "משבצות השעות" (עם ציון / בלי ציון) יושבות תמיד צמוד לתאריך
                    # התחלה - לא מספיק לחפש כל טוקן שנראה כמו מספר בכל השורה, כי שם
                    # הקורס עצמו יכול להכיל מספר (למשל "הכשרת המחנך החדש-5643").
                    hour_slot_size = 0
                    for t in reversed(before):
                        if t == "--" or t.replace(".", "", 1).isdigit():
                            hour_slot_size += 1
                            if hour_slot_size == 2:
                                break
                        else:
                            break
                    hour_tokens = before[len(before) - hour_slot_size:] if hour_slot_size else []
                    hours = None
                    for t in hour_tokens:
                        if t != "--":
                            hours = float(t)
                            break

                    label_tail = before[: len(before) - hour_slot_size]
                    label = " ".join((pending_label + [" ".join(label_tail)]))[-200:].strip()
                    pending_label = []

                    oz_letmura_flag = after[1] if len(after) > 1 else None
                    source_type_guess = "school_based" if ("בית ספרית" in label or "בית-ספרית" in label) else "other"
                    pre_approved_guess = bool(oz_letmura_flag and oz_letmura_flag != "--")

                    if hours is not None:
                        rows.append({
                            "label": label,
                            "start_date": _to_iso_date(dates[0]),
                            "end_date": _to_iso_date(dates[1]),
                            "hours": hours,
                            "source_type_guess": source_type_guess,
                            "pre_approved_guess": pre_approved_guess,
                        })
                elif seen_header:
                    pending_label.append(joined)
                    pending_label = pending_label[-3:]

    return rows
