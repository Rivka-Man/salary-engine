"""
"מה עכשיו": להפוך כל פנייה ללחיצה אחת, ולהכניס תזכורת ליומן.

- פנייה מקוונת לחשבות השכר: הכפתור מעתיק את הנוסח ופותח את פורטל עובדי הוראה,
  עם הנחיות איך מגיעים לטופס הפנייה (לפי צילומי המסך של הפורטל).
- מייל למוסד ההשתלמות: קישורים שפותחים מייל חדש ב-Gmail או ב-Outlook, עם הנושא
  והנוסח כבר בפנים. mailto בנוסף, למי שיש תוכנת דואר.
- תזכורת: קישורים שמוסיפים אירוע ל-Google Calendar או ל-Outlook, בלי קובץ להורדה.

הנוסח כולל את ID_PLACEHOLDER במקום תעודת הזהות. המסך מחליף אותו בדפדפן במה
שהמורה מקלידה, ובונה מחדש את הקישורים עם build_* בצד הלקוח (ראו contracts.py).
תעודת הזהות לא נשלחת לשרת.
"""
import os
from datetime import date, timedelta
from urllib.parse import quote, urlencode

PORTAL_URL = "https://poh.education.gov.il"
# הכתובת של עמוד "פתיחת פנייה" בפורטל (מאחורי ההתחברות). כשמישהי עם כניסה לפורטל מעתיקה אותה
# משורת הכתובת, שמים אותה כאן או במשתנה הסביבה, והכפתור יגיע ישר לטופס.
# לאן נשלח המייל על קורס שלא דווח
COURSE_EMAIL = os.environ.get("COURSE_EMAIL") or "ptzibur_ka@education.gov.il"
PORTAL_REQUEST_URL = os.environ.get("PORTAL_REQUEST_URL") or "https://pny.education.gov.il"  # מערכת הפניות; מפנה להתחברות ומשם לטופס
HOTLINE = "*6552"

# איך מגיעים לטופס פנייה מקוונת (פס "שירות ותמיכה" בפורטל)
PORTAL_REQUEST_STEPS = [
    "נכנסים לפורטל עובדי הוראה עם הסיסמה האישית.",
    "בפס הכחול העליון לוחצים \"שירות ותמיכה\", ואז \"פתיחת פנייה\".",
    "בוחרים נושא \"שכר\" ותת-נושא שקשור לגמולים. [רבקה משלימה את השמות המדויקים]",
    "בשדה התיאור מדביקים את הנוסח (Ctrl+V). הוא כבר הועתק בלחיצה.",
    "מצרפים את התלוש ואת \"הקורסים שלי\", ושולחים.",
    "רושמים את מספר הפנייה שמתקבל, ומזינים אותו כאן.",
]


def gmail_url(to, subject, body):
    return "https://mail.google.com/mail/?" + urlencode({"view": "cm", "fs": "1", "to": to or "", "su": subject, "body": body})


def outlook_url(to, subject, body):
    return "https://outlook.office.com/mail/deeplink/compose?" + urlencode({"to": to or "", "subject": subject, "body": body})


def mailto_url(to, subject, body):
    return f"mailto:{quote(to or '')}?subject={quote(subject)}&body={quote(body)}"


def send_options(track, subject, body, to_email=None, link=None):
    """
    הדרכים לשלוח פנייה אחת, לכרטיס הפנייה.
    track: payroll | ministry | institution | unique | hotline | document | fix_data
    """
    if track in ("payroll", "ministry"):
        return {"kind": "portal", "url": PORTAL_REQUEST_URL or link or PORTAL_URL, "copy_first": True,  # מערכת הפניות קודמת לקישור הכללי
                "label": "פתיחת פנייה מקוונת", "steps": PORTAL_REQUEST_STEPS,
                "followup_days": 30,
                "followup": f"אם אין מענה תוך 30 יום, מתקשרים למוקד חשבות השכר {HOTLINE} עם מספר הפנייה."}
    if track in ("institution", "unique"):
        to_email = to_email or COURSE_EMAIL
        return {"kind": "email", "to": to_email or "", "label": "פתיחת מייל",
                "gmail": gmail_url(to_email, subject, body), "outlook": outlook_url(to_email, subject, body),
                "mailto": mailto_url(to_email, subject, body),
                "note": "את כתובת המייל של המזכירות אפשר למצוא באתר המוסד או באישור הסיום של הקורס." if not to_email else ""}
    if track == "hotline":
        return {"kind": "phone", "phone": HOTLINE, "label": f"מוקד {HOTLINE}"}
    return {"kind": "none"}


# ------------------------------------------------------------------
# תזכורת
# ------------------------------------------------------------------

def _next_payslip_check(today=None):
    """התלוש של החודש מתפרסם בתחילת החודש הבא. בודקים ב-10 בחודש הבא."""
    today = today or date.today()
    y, m = (today.year + (today.month // 12), today.month % 12 + 1)
    return date(y, m, 10)


def _gcal_url(title, day, details):
    d1, d2 = day.strftime("%Y%m%d"), (day + timedelta(days=1)).strftime("%Y%m%d")
    return "https://calendar.google.com/calendar/render?" + urlencode(
        {"action": "TEMPLATE", "text": title, "dates": f"{d1}/{d2}", "details": details})


def _outlook_cal_url(title, day, details):
    return "https://outlook.office.com/calendar/0/deeplink/compose?" + urlencode(
        {"subject": title, "startdt": day.isoformat(), "enddt": (day + timedelta(days=1)).isoformat(),
         "allday": "true", "body": details, "path": "/calendar/action/compose"})


def reminder_card(link_url="", sent_on=None, today=None):
    """
    כרטיס תזכורת: בדיקת התלוש הבא, ומעקב אחרי 30 יום אם אין מענה.
    link_url: הקישור האישי של המורה ב-OptiPay, כדי לחזור לאותה נקודה.
    """
    today = today or date.today()
    check_day = _next_payslip_check(today)
    follow_day = (sent_on or today) + timedelta(days=30)
    back = f"\n\nחזרה ל-OptiPay: {link_url}" if link_url else ""
    t1 = "OptiPay: לבדוק אם גמול ההשתלמות תוקן בתלוש"
    d1 = "גוררים את התלוש החדש ל-OptiPay, והבדיקה רצה לבד." + back
    t2 = "OptiPay: מעקב אחרי הפנייה לחשבות השכר"
    d2 = f"אם עדיין אין מענה, מתקשרים למוקד חשבות השכר {HOTLINE} עם מספר הפנייה." + back
    return {
        "type": "reminder",
        "title": "תזכורת לחודש הבא",
        "items": [
            {"key": "payslip", "label": "לבדוק את התלוש הבא", "date": check_day.isoformat(),
             "google": _gcal_url(t1, check_day, d1), "outlook": _outlook_cal_url(t1, check_day, d1)},
            {"key": "followup", "label": "מעקב אם אין מענה", "date": follow_day.isoformat(),
             "google": _gcal_url(t2, follow_day, d2), "outlook": _outlook_cal_url(t2, follow_day, d2)},
        ],
    }
