"""
read_file: מזהה איזה מסמך המורה גררה, קורא אותו ושומר את הנתונים.

המורה לא מסמנת מה כל קובץ. מנסים את הקריאות הקיימות לפי הסדר, והראשונה
שמחזירה נתונים קובעת את הסוג. PDF עם טקסט: הצטרפות לעוז, מכתב העסקה, הקורסים
שלי. תמונה, או PDF שאף קריאה לא זיהתה: Claude קורא אותו כתלוש. אם גם זה לא
תלוש: "לא שייך".

השמירה לטבלאות זהה ל-app.my_wizard_docs_upload, עם הבדל אחד: ייבוא "הקורסים
שלי" מחליף רק קורסים שהגיעו מהפורטל, ולא מוחק קורסים שהמורה הזינה בעצמה
(הם בדיוק מה שמפעיל את סיבה B).
"""
import io
import json
from datetime import datetime

import payslips
from engine import school_year_of
from pdf_import import parse_courses_pdf, parse_maktav_haasaka_pdf, parse_oz_join_pdf

from agent import demo_docs
from agent.store import DOC_LABELS, get_document, set_doc_type

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")


def _dmy(iso):
    s = str(iso or "")
    return f"{int(s[8:10])}.{int(s[5:7])}.{s[:4]}" if len(s) >= 10 and s[4] == "-" else s


def _try(parser, data):
    try:
        return parser(io.BytesIO(data))
    except Exception:
        return None


def detect(filename, data, extract_payslip=None):
    """
    מזהה את סוג הקובץ ומחזיר (doc_type, parsed). parsed הוא מה שהקריאה החזירה.
    extract_payslip ניתן להחלפה בבדיקות.
    """
    if demo_docs.is_demo(data):  # המסלול הירוק: קובץ דמו בלחיצה (agent/demo_docs.py)
        return demo_docs.parse(data)
    name = (filename or "").lower()
    is_pdf = name.endswith(".pdf") or data[:5] == b"%PDF-"
    if is_pdf:
        found = _try(parse_oz_join_pdf, data)
        if found and found.get("reform_start_date"):
            return "oz_join", found
        found = _try(parse_maktav_haasaka_pdf, data)
        if found:
            return "maktav", found
        rows = _try(parse_courses_pdf, data)
        if rows:
            return "courses", rows
    if is_pdf or name.endswith(IMAGE_EXT):
        extract = extract_payslip or payslips.extract_payslip
        try:
            return "payslip", extract(filename, data)
        except payslips.PayslipError as exc:
            msg = str(exc)
            if "לא נראה כמו תלוש" in msg:
                return "unknown", None
            return "payslip_failed", {"error": msg}
    return "unknown", None


# ------------------------------------------------------------------
# שמירה לטבלאות, לפי סוג
# ------------------------------------------------------------------

def _save_oz_join(conn, tid, found):
    d = found["reform_start_date"]
    conn.execute("UPDATE teachers SET reform_start_date=%s, reform_start_year=%s, in_reform=1 WHERE teacher_id=%s",
                 (d, school_year_of(d), tid))
    return {"reform_start_date": d}, f"משרה ראשונה בעוז: {_dmy(d)}"


def _save_maktav(conn, tid, found):
    sets, params, notes = [], [], []
    if found.get("certification_date"):
        sets.append("certification_date=%s"); params.append(found["certification_date"])
    for key, flag, label in (
        ("double_degree_start_date", "has_double_degree_gimul", "כפל תואר"),
        ("additional_double_degree_start_date", "has_additional_double_degree_gimul", "כפל תואר נוסף"),
        ("beinayim_start_date", "has_beinayim_gimul", "גמול ביניים"),
    ):
        if found.get(key):
            sets += [f"{key}=%s", f"{flag}=1"]; params.append(found[key])
            notes.append(f"{label} מ-{_dmy(found[key])}")
    if sets:
        params.append(tid)
        conn.execute(f"UPDATE teachers SET {', '.join(sets)} WHERE teacher_id=%s", tuple(params))
    return found, (" · ".join(notes) or "בלי גמול כפל תואר או ביניים")


def _save_courses(conn, tid, rows):
    conn.execute("DELETE FROM courses WHERE teacher_id=%s AND source='portal'", (tid,))
    years = []
    for r in rows:
        try:
            sy = school_year_of(r["start_date"])
        except Exception:
            sy = None
        if sy:
            years.append(r["start_date"][:4])
        conn.execute(
            """INSERT INTO courses (teacher_id, start_date, end_date, description, school_year, hours, hours_graded,
                                    hours_ungraded, source_type, graded, grade, pre_approved,
                                    is_advanced_degree_track, counts_for_oz, source)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0,NULL,1,0,%s,'portal')""",
            (tid, r["start_date"], r.get("end_date"), r.get("name"), sy, float(r["hours"]), r.get("hours_graded"),
             r.get("hours_ungraded"), r.get("source_type_guess", "institutional"),
             1 if r.get("counts_for_oz", True) else 0),
        )
    span = f" · {min(years)}–{max(years)}" if years else ""
    return {"courses": len(rows)}, f"{len(rows)} קורסים{span}"


def _save_vacations(conn, tid, by_year):
    """עמוד "חל"ת ושבתון" בפורטל: {שנת לימודים: סטטוס}."""
    for sy, st in by_year.items():
        conn.execute(
            """INSERT INTO employment_status_by_year (teacher_id, school_year, status) VALUES (%s,%s,%s)
               ON CONFLICT (teacher_id, school_year) DO UPDATE SET status=EXCLUDED.status""", (tid, sy, st))
    special = [f"{'שבתון' if st == 'שבתון' else st} {sy}" for sy, st in by_year.items() if st != "רגיל"]
    return {"years": by_year}, (" · ".join(special) or "בלי שבתון או חל\"ת")


def _save_payslip(conn, tid, doc_id, ext):
    month = ext.get("pay_month")
    gimul = ext.get("gimul") or {}
    if not month:
        return None, "לא זוהה חודש התלוש"
    conn.execute("DELETE FROM payslips WHERE teacher_id=%s AND pay_month=%s", (tid, month))
    row = conn.execute(
        """INSERT INTO payslips (teacher_id, pay_month, gimul_code, gimul_desc, gimul_units, gimul_amount,
                                 doc_id, extraction_json, run_id, created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s) RETURNING payslip_id""",
        (tid, month, gimul.get("code"), gimul.get("description"), gimul.get("units"), gimul.get("amount"),
         doc_id, json.dumps(ext, ensure_ascii=False), datetime.now().isoformat(timespec="seconds")),
    ).fetchone()
    units = gimul.get("units")
    line = f"שורה 249: {payslips._fmt(units)} יח'" if units is not None else "אין שורת גמול השתלמות (249)"
    return {"payslip_id": row["payslip_id"], "pay_month": month, "units": units}, f"{payslips.month_label(month)} · {line}"


def read_file(conn, teacher_id, doc_id, extract_payslip=None):
    """
    הכלי read_file. מחזיר:
    {"ok", "doc_id", "doc_type", "label", "detail", "saved", "problems"}
    doc_type: payslip | courses | oz_join | maktav | unknown
    """
    doc = get_document(conn, teacher_id, doc_id)
    if not doc:
        return {"ok": False, "doc_id": doc_id, "doc_type": "unknown", "label": "קובץ לא נמצא",
                "detail": "", "saved": {}, "problems": ["הקובץ לא נמצא בתיק."]}
    kind, parsed = detect(doc["filename"], doc["content"], extract_payslip)
    out = {"ok": True, "doc_id": doc_id, "doc_type": kind, "label": DOC_LABELS.get(kind, doc["filename"]),
           "detail": "", "saved": {}, "problems": [], "filename": doc["filename"]}
    try:
        if kind == "oz_join":
            out["saved"], out["detail"] = _save_oz_join(conn, teacher_id, parsed)
        elif kind == "maktav":
            out["saved"], out["detail"] = _save_maktav(conn, teacher_id, parsed)
        elif kind == "courses":
            out["saved"], out["detail"] = _save_courses(conn, teacher_id, parsed)
        elif kind == "vacations":
            out["saved"], out["detail"] = _save_vacations(conn, teacher_id, parsed)
        elif kind == "payslip":
            saved, detail = _save_payslip(conn, teacher_id, doc_id, parsed)
            out["detail"] = detail
            if saved:
                out["saved"] = saved
            else:
                out["ok"] = False
                out["problems"].append("לא הצלחתי לזהות את חודש התלוש.")
        elif kind == "payslip_failed":
            out.update(ok=False, doc_type="payslip", label=DOC_LABELS["payslip"],
                       detail="לא נקרא", problems=[parsed["error"]])
        else:
            out.update(ok=False, label=doc["filename"], detail="לא שייך לבדיקת הגמול",
                       problems=["הקובץ לא נראה כמו תלוש או קובץ מהפורטל."])
        set_doc_type(conn, doc_id, out["doc_type"])
        conn.commit()
    except Exception as exc:
        conn.rollback()
        out.update(ok=False, detail="לא נקרא", problems=[f"שגיאה בשמירה: {exc.__class__.__name__}"])
    return out
