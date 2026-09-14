"""
ייבוא קורסים מקובץ PDF שהורד מפורטל עו"ה (עמוד "הקורסים שלי").
הפורטל לא מייצא PDF עם טבלה אמיתית (רק טקסט בפריסת הדפסה), והעברית
בתוכו שמורה בכיוון הפוך תו-אחר-תו - לכן הפענוח מבוסס על מיקום מילים
בעמוד (לא על extract_table הרגיל של pdfplumber, שמפיק תאים מבולגנים
על הקובץ הזה).

מה שכן ניתן לשלוף באמינות: תאריך תחילה/סיום, ושעות עוז לתמורה כפי
שמופיעות בעמודת "משתתף בגמולי עוז לתמורה" עצמה (לא "היקף שעות עם/בלי
ציון" הכללי) - זה המספר הרלוונטי לחישוב של המנוע הזה. שם הקורס והמוסד
מפוצלים על בסיס מילות-מפתח של מוסדות מוכרים (מרכז פסג"ה / מכללה /
מכון / השתלמות בית ספרית) - ניחוש סביר אך לא מובטח, ולכן ניתנים
לעריכה בתצוגה המקדימה לפני שמירה. הסכמה של courses לא כוללת שדה
מוסד בכלל, כך שהוא מוצג לצורך זיהוי בלבד ולא נשמר.
"""
import re

import pdfplumber

HEBREW_RE = re.compile(r"[֐-׿]")
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
HEADER_MARKERS = ("שם וסמל", "היקף", "משתתף", "תאריך", "מוסד", "הקורסים", "נתונים", "פיתוח", "לפירוט")
INSTITUTION_MARKERS = ("מרכז", "פסג", "מכללה", "מכון", "השתלמות")


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


def _split_name_institution(tokens):
    for i, t in enumerate(tokens):
        if any(t.startswith(m) or m in t for m in INSTITUTION_MARKERS):
            return " ".join(tokens[:i]).strip(), " ".join(tokens[i:]).strip()
    return " ".join(tokens).strip(), ""


def _to_iso_date(ddmmyyyy):
    parts = re.split(r"[/.]", ddmmyyyy.strip("."))
    d, m, y = parts[0], parts[1], parts[2]
    return f"{y}-{m.zfill(2)}-{d.zfill(2)}"


# --- פענוח "מכתב העסקה" ---
# הגופן במכתב ההעסקה שמור בלי מיפוי Unicode תקין, ו-pdfplumber מוציא רק
# קודי glyph גולמיים בצורת "(cid:N)". התברר שהמיפוי לינארי וקבוע:
# codepoint = cid + 1334 עבור כל אותיות עברית (U+05D0..U+05EA), כלומר
# ניתן לפענח כל אות. תווי הגלִיף מסודרים בסדר ויזואלי (הפוך ללוגי), לכן
# הופכים כל רצף עברית אחרי הפענוח. מספרים/תאריכים מחולצים כרגיל.
_CID_RE = re.compile(r"\(cid:(\d+)\)")
_MAKTAV_DATE_RE = re.compile(r"^\.?\d{1,2}/\d{1,2}/\d{4}$")


def _cid_char(n):
    n = int(n)
    if 154 <= n <= 180:
        return chr(n + 1334)
    return ""  # תווים שאינם עברית (מרכאות, סימני RTL) - מושמטים


def _decode_maktav(text):
    s = _CID_RE.sub(lambda m: _cid_char(m.group(1)), text)
    # היפוך רצפי עברית (ויזואלי -> לוגי)
    return re.sub(r"[֐-׿]+", lambda mm: mm.group(0)[::-1], s)


def _maktav_rows(pdf):
    """מחזיר לכל עמוד רשימת שורות; כל שורה = list של (x0, decoded_text)
    ממוינת מימין לשמאל (x0 יורד)."""
    for page in pdf.pages:
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        buckets = {}
        for w in words:
            if w["text"] == "!":
                continue
            key = round(w["top"] / 4) * 4
            dec = _decode_maktav(w["text"])
            if dec.strip():
                buckets.setdefault(key, []).append((w["x0"], dec))
        for key in sorted(buckets):
            yield sorted(buckets[key], key=lambda t: -t[0])


def parse_maktav_haasaka_pdf(file_path_or_buffer):
    """
    שולף מ"מכתב העסקה" (מהפורטל) את:
      - certification_date: התאריך שאחרי "מורה מוסמך מתאריך:"
      - double_degree_start_date: "החל מ" בשורת "כפל תואר"
      - additional_double_degree_start_date: בשורת "כפל תואר נוסף"
      - beinayim_start_date: בשורת "גמול ביניים"
    מחזיר dict רק עם המפתחות שנמצאו.
    """
    found = {}
    with pdfplumber.open(file_path_or_buffer) as pdf:
        for row in _maktav_rows(pdf):
            joined = " ".join(t for _, t in row)
            dates = [t.strip(".") for _, t in row if _MAKTAV_DATE_RE.match(t)]

            if "certification_date" not in found and "מוסמך" in joined and "מתאריך" in joined and dates:
                found["certification_date"] = _to_iso_date(dates[-1])

            # שורות טבלת "הנושא": התווית בעמודה הימנית ביותר (x0 גבוה)
            label = " ".join(t for _, t in row[:3])
            date_hachel = None
            # "החל מ" הוא עמודת התאריך עם ה-x0 הגבוה מבין תאריכי השורה
            row_dates = [(x0, t.strip(".")) for x0, t in row if _MAKTAV_DATE_RE.match(t)]
            if row_dates:
                date_hachel = max(row_dates, key=lambda p: p[0])[1]

            if date_hachel:
                if "כפל" in label and "תואר" in label and "נוסף" in label:
                    found.setdefault("additional_double_degree_start_date", _to_iso_date(date_hachel))
                elif "כפל" in label and "תואר" in label:
                    found.setdefault("double_degree_start_date", _to_iso_date(date_hachel))
                elif "ביניים" in label or "בינים" in label:
                    found.setdefault("beinayim_start_date", _to_iso_date(date_hachel))
    return found


def parse_heikef_misra_pdf(file_path_or_buffer):
    """
    שולף את "תאריך הצטרפות לעוז לתמורה" מקובץ PDF שהופק מעמוד "היקף משרה"
    בפורטל עו"ה. אותה בעיית קידוד עברית הפוכה תו-אחר-תו כמו בקובץ
    הקורסים - וגם כאן העמוד בנוי כרשת של תוויות/ערכים לפי מיקום (לא
    טבלה אמיתית), עם כמה "עמודות" זו לצד זו באותה שורה. במקום לנתח את
    כל הרשת, מאתרים ספציפית את התווית "תאריך הצטרפות לעוז [שורה הבאה]
    לתמורה" ולוקחים את התאריך הראשון שמופיע מתחתיה **באותו טווח X**
    (כדי לא להתבלבל עם "תאריך הצטרפות לאופק חדש" שמופיע בעמודה אחרת).

    מחזיר {"reform_start_date": ISO date} או {} אם לא נמצא.
    """
    with pdfplumber.open(file_path_or_buffer) as pdf:
        for page in pdf.pages:
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            rows = {}
            for w in words:
                if w["text"] == "!":
                    continue
                key = round(w["top"] / 3) * 3
                rows.setdefault(key, []).append((w["x0"], _fix_word(w["text"])))

            label_row = next(
                (r for r, items in rows.items()
                 if {"תאריך", "הצטרפות", "לעוז"} <= {t for _, t in items}),
                None,
            )
            if label_row is None:
                continue

            x_taarich = next(x0 for x0, t in rows[label_row] if t == "תאריך")
            x_leoz = next(x0 for x0, t in rows[label_row] if t == "לעוז")
            col_lo, col_hi = x_leoz - 20, x_taarich + 20

            cont_row = next(
                (r for r in sorted(rows) if label_row < r <= label_row + 40
                 and any(t == "לתמורה" and col_lo - 30 <= x0 <= col_hi + 30 for x0, t in rows[r])),
                None,
            )
            if cont_row is None:
                continue

            for r in sorted(rows):
                if r <= cont_row or r > cont_row + 40:
                    continue
                for x0, t in rows[r]:
                    if DATE_RE.match(t) and col_lo - 60 <= x0 <= col_hi + 60:
                        return {"reform_start_date": _to_iso_date(t)}
    return {}


def parse_courses_pdf(file_path_or_buffer):
    """
    מחזיר רשימת dict-ים, אחד לכל קורס שיש לו ערך בעמודת "משתתף בגמולי
    עוז לתמורה" (מספר שעות, לא "--"). קורסים בלי ערך שם לא מוחזרים
    כלל - הם לא נספרים לעניין זה לפי הפורטל עצמו.

    כל item: {name, institution, start_date (ISO), end_date (ISO,
    לתצוגה בלבד - אין בסכמה שדה כזה), hours, source_type_guess}
    """
    rows = []
    pending_tokens = []
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
                    before = tokens[:idx1]
                    after = tokens[tokens.index(dates[1]) + 1:]

                    # שני "משבצות השעות" (עם ציון / בלי ציון) יושבות תמיד צמוד
                    # לתאריך התחלה - לא מספיק לחפש כל טוקן שנראה כמו מספר בכל
                    # השורה, כי שם הקורס עצמו יכול להכיל מספר (למשל קורס
                    # "הכשרת המחנך החדש-5643"). לא משתמשים בערך שלהן בפועל -
                    # רק כדי לדעת איפה נגמר שם הקורס/המוסד ומתחילות השעות.
                    hour_slot_size = 0
                    for t in reversed(before):
                        if t == "--" or t.replace(".", "", 1).isdigit():
                            hour_slot_size += 1
                            if hour_slot_size == 2:
                                break
                        else:
                            break

                    label_tail = before[: len(before) - hour_slot_size]
                    name, institution = _split_name_institution(pending_tokens + label_tail)
                    pending_tokens = []

                    # שתי משבצות השעות הכלליות: before[-2]=עם ציון, before[-1]=בלי ציון
                    def _hval(tok):
                        if not tok or tok == "--":
                            return None
                        try:
                            return float(tok)
                        except ValueError:
                            return None
                    hours_graded = _hval(before[-2]) if len(before) >= 2 else None
                    hours_ungraded = _hval(before[-1]) if len(before) >= 1 else None

                    # השעות נלקחות מעמודת "משתתף בגמולי עוז לתמורה" עצמה
                    # (הטוקן האחרון בשורה) - זה המספר הרלוונטי לחישוב עוז
                    # לתמורה, לא "היקף שעות עם/בלי ציון" הכללי. קורס בלי ערך
                    # שם ("--") לא נספר לעניין זה ולא מוחזר בכלל.
                    oz_letmura_value = after[1] if len(after) > 1 else None
                    hours = None
                    if oz_letmura_value and oz_letmura_value != "--":
                        try:
                            hours = float(oz_letmura_value)
                        except ValueError:
                            hours = None

                    if hours is not None:
                        source_type_guess = "school_based" if "השתלמות" in institution else "other"
                        rows.append({
                            "name": name,
                            "institution": institution,
                            "start_date": _to_iso_date(dates[0]),
                            "end_date": _to_iso_date(dates[1]),
                            "hours": hours,
                            "hours_graded": hours_graded,
                            "hours_ungraded": hours_ungraded,
                            "source_type_guess": source_type_guess,
                        })
                elif seen_header:
                    pending_tokens.extend(tokens)
                    pending_tokens = pending_tokens[-15:]

    return rows
