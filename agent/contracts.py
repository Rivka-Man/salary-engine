"""
החוזה בין מסך השיחה (יהודית, app.py) לבין הסוכן (חוי, agent/).

משתנה רק בהסכמה של שתינו. עד שהסוכן האמיתי מוכן, המסך עובד מול demo_run_agent
שמחזיר בדיוק את אותו מבנה, לפי התרחיש של שרה כהן.

--- מה המסך קורא ---

    from agent import run_agent, save_upload

    doc_id = save_upload(teacher_id, request.files["f"])        # לכל קובץ שנגרר
    out = run_agent(teacher_id, {"type": "files", "doc_ids": [17, 18, 19]})
    out = run_agent(teacher_id, {"type": "answer", "value": "כן, שבתון"})
    out = run_agent(teacher_id, {"type": "message", "text": "למה הקורס לא נספר?"})
    out = run_agent(teacher_id, {"type": "resume"})                # המורה חזרה לקישור
    out = run_agent(teacher_id, {"type": "sent", "track": "payroll"})  # לחצה "שלחי"

--- מה חוזר (תמיד אותו מבנה, גם מרשת הביטחון) ---

    {
      "status":   "reply" | "question" | "error",
      "did":      ["קראתי 3 קבצים", "הרצתי את המנוע", "הכנתי 2 פניות"],  # השורות הקטנות מעל התשובה
      "text":     "עד שלושה משפטים",
      "cards":    [CARD, ...],
      "case":     CASE,                  # "התיק שלי"
      "fallback": False,                 # True = רשת הביטחון ענתה, בלי AI
    }

--- כרטיסים ---

    {"type": "file", "doc_id": 17, "doc_type": "courses", "label": "הקורסים שלי",
     "detail": "5 קורסים · 2021–2026", "ok": True}
    {"type": "file", "doc_id": 20, "doc_type": "unknown", "label": "תעודת_הוקרה.pdf",
     "detail": "לא שייך לבדיקת הגמול", "ok": False}
    {"type": "guide", "doc_type": "oz_join" | "vacations" | ..., "label": "הצטרפות לעוז לתמורה",
     "portal_path": "מרחב מנהלי ← ...", "image": "oz_join.png", "why": "...",
     "steps": ["נכנסים לפורטל...", "בפס הכחול העליון לוחצים \"מרחב מנהלי\".", ...]}  # להציג כרשימה ממוספרת
    {"type": "question", "field": "sabbatical", "school_year": "תשפ\"ה",
     "text": "היית בשבתון או בחל\"ת בתשפ\"ה?", "options": ["כן, שבתון", "כן, חל\"ת", "לא", "לא יודעת"], "why": "..."}
     # "לא יודעת" ← כרטיס guide לקובץ החופשות, ולא כרטיס תוצאה
    {"type": "result", "ok": False, "expected": 26.75, "paid": 22.75, "gap_units": 4, "gap_ils": 97.08,
     "pay_month": "2026-09",
     "parts": [{"units": 3, "label": "השתלמויות שנת תשפ\"ה (שבתון)", "rule_id": "R12_SABBATICAL_EXEMPT",
                "source": "הסכם עוז לתמורה, סעיף 18(ה)", "quote": "..." | None}],
     "slip_url": "/my/<token>/payslip", "how_url": "/my/<token>/payslip?open=how"}
    {"type": "action", "track": "payroll", "to": "חשבות השכר", "title": "פנייה לחשבות השכר",
     "channel": "פורטל עובדי הוראה · פניות מקוונות", "link": "https://poh.education.gov.il",
     "subject": "...", "body": "... ת\"ז [להשלים] ...", "attach": ["צילום התלוש"],
     "todo_url": "/my/<token>/payslip/<id>/todo"}

      + "send": איך שולחים בלחיצה אחת (agent/send.py):
        {"kind": "portal", "url": "https://poh.education.gov.il", "copy_first": True,
         "label": "פתיחת פנייה מקוונת", "steps": [...], "followup": "..."}
            ← הכפתור מעתיק את הנוסח ללוח (navigator.clipboard בתוך ה-click) ופותח את הפורטל בלשונית חדשה
        {"kind": "email", "to": "", "gmail": "...", "outlook": "...", "mailto": "...", "note": "..."}
            ← כפתורי "פתיחה ב-Gmail" / "פתיחה ב-Outlook" (target=_blank). mailto רק כגיבוי
        {"kind": "phone", "phone": "*6552"} | {"kind": "none"}
      + "sent": True אם המורה כבר סימנה שנשלח

    ב-body של פנייה מופיע ID_PLACEHOLDER (playbook.ID_PLACEHOLDER). המסך מחליף אותו
    בדפדפן בלבד במה שהמורה מקלידה, ובונה מחדש את קישורי Gmail/Outlook עם הנוסח
    המעודכן (אותו מבנה כמו agent/send.gmail_url). תעודת הזהות לא נשלחת לשרת.

    {"type": "reminder", "title": "תזכורת לחודש הבא",
     "items": [{"key": "payslip", "label": "לבדוק את התלוש הבא", "date": "2026-12-10",
                "google": "https://calendar.google.com/...", "outlook": "https://outlook.office.com/..."},
               {"key": "followup", "label": "מעקב אם אין מענה", "date": "...", "google": "...", "outlook": "..."}]}
        ← יוצא יחד עם כרטיסי הפנייה, ושוב אחרי שכל הפניות סומנו כנשלחו

    אירוע "נשלח": {"type": "sent", "track": "payroll", "ref": "12345"}   # ref = מספר פנייה, לא חובה
    בתיק: case["sent_info"] = [{"track": "payroll", "ref": "12345", "on": "2026-11-26"}]

--- התיק ---

    CASE = {
      "docs":  [{"doc_type": "payslip", "label": "תלוש שכר", "status": "read" | "missing" | "failed",
                 "detail": "ספטמבר 2026 · 22.75 יח'"}, ...],
      "facts": [{"key": "sabbatical:תשפ\"ה", "label": "שבתון תשפ\"ה", "value": "שבתון",
                 "source": "manual" | "portal" | "payslip"}, ...],
      "result": {"gap_units": 4, "gap_ils": 97.08, "ok": False} | None,
      "sent":  ["payroll"],
      "progress": 70,                     # 0-100, לפס ההתקדמות
    }

--- מה המסך מספק לסוכן (יהודית) ---

    save_upload(teacher_id, file_storage) -> int      # agent/uploads.py
    portal_guide(doc_type) -> dict                     # agent/portal_guide.json

    עד שהקבצים האלה קיימים, agent/ משתמש בגרסה פנימית של שניהם (ראו agent/store.py).
"""

ID_PLACEHOLDER = '[ת"ז — להשלים]'  # זהה ל-playbook.ID_PLACEHOLDER


def demo_run_agent(teacher_id, event):
    """
    גרסת דמה של run_agent, לפי התרחיש של שרה כהן. לבניית המסך לפני שהסוכן מוכן.
    files ← כרטיסי קובץ + שאלת שבתון. answer ← תוצאה + שתי פניות.
    """
    case = {
        "docs": [{"doc_type": "payslip", "label": "תלוש שכר", "status": "read", "detail": "ספטמבר 2026 · 22.75 יח'"},
                 {"doc_type": "courses", "label": "הקורסים שלי", "status": "read", "detail": "5 קורסים"},
                 {"doc_type": "oz_join", "label": "הצטרפות לעוז לתמורה", "status": "read", "detail": "1.9.2011"}],
        "facts": [{"key": "course:manual", "label": "קורס תשפ\"ו, 112 שעות", "value": "112", "source": "manual"}],
        "result": None, "sent": [], "progress": 70,
    }
    kind = (event or {}).get("type")
    if kind == "files":
        return {
            "status": "question", "fallback": False,
            "did": ["זיהיתי 4 קבצים", "קראתי 3", "קובץ אחד לא שייך", "בדקתי מה חסר"],
            "text": "קראתי את הקבצים. בתלוש מוכרות לך 22.75 יחידות. בתשפ\"ה למדת 336 שעות, יותר מהתקרה של יחידה לשנה, אז יש לי שאלה אחת:",
            "cards": [
                {"type": "file", "doc_id": 1, "doc_type": "payslip", "label": "תלוש שכר", "detail": "ספטמבר 2026 · שורה 249: 22.75 יח'", "ok": True},
                {"type": "file", "doc_id": 2, "doc_type": "courses", "label": "הקורסים שלי", "detail": "5 קורסים · 2021–2026", "ok": True},
                {"type": "file", "doc_id": 3, "doc_type": "oz_join", "label": "הצטרפות לעוז לתמורה", "detail": "משרה ראשונה בעוז: 1.9.2011", "ok": True},
                {"type": "file", "doc_id": 4, "doc_type": "unknown", "label": "תעודת_הוקרה_2024.pdf", "detail": "לא שייך לבדיקת הגמול", "ok": False},
                {"type": "question", "field": "sabbatical", "school_year": "תשפ\"ה",
                 "text": "היית בשבתון או בחל\"ת בתשפ\"ה?", "options": ["כן, שבתון", "כן, חל\"ת", "לא", "לא יודעת"],
                 "why": "בשבתון ובחל\"ת התקרה של יחידה לשנה לא חלה."},
            ],
            "case": case,
        }
    if kind == "answer" and "לא יודעת" in str(event.get("value")):
        return {
            "status": "reply", "fallback": False,
            "did": ["בדקתי איפה זה מופיע", "הכנתי הדרכה: חל\"ת ושבתון"],
            "text": "אין בעיה. אם תשפ\"ה הייתה שנת שבתון או חל\"ת רואים בעמוד \"חל\"ת ושבתון\" בפורטל. הנה בדיוק איך מגיעים אליו. גררי לכאן את ה-PDF, ואבדוק לבד.",
            "cards": [{"type": "guide", "doc_type": "vacations", "label": "חל\"ת ושבתון",
                       "portal_path": "מרחב מנהלי ← פרטי העסקה ← חופשות והעברות ← חל\"ת ושבתון ← בחירת שנה ← הדפס",
                       "steps": ["נכנסים לפורטל עובדי הוראה עם הסיסמה האישית.",
                                 "בפס הכחול העליון לוחצים \"מרחב מנהלי\", ואז בתפריט \"פרטי העסקה\".",
                                 "מעבירים את העכבר על \"חופשות והעברות\", ובוחרים \"חל\"ת ושבתון\".",
                                 "בתיבה \"שנה\" בוחרים תשפ\"ה.",
                                 "לוחצים \"הדפס\" (משמאל למעלה), ובחלון ההדפסה בוחרים \"שמירה כ-PDF\"."],
                       "image": None, "why": "ממנו רואים באילו שנים היית בשבתון, בחצי שבתון או בחל\"ת."}],
            "case": case,
        }
    if kind == "answer":
        case["facts"].append({"key": "sabbatical:תשפ\"ה", "label": "שבתון תשפ\"ה", "value": "שבתון", "source": "manual"})
        case["result"] = {"gap_units": 4, "gap_ils": 97.08, "ok": False}
        case["progress"] = 100
        return {
            "status": "reply", "fallback": False,
            "did": ["שמרתי: שבתון תשפ\"ה", "הרצתי את המנוע", "השוויתי לתלוש", "הכנתי 2 פניות"],
            "text": "בתלוש חסרות לך 4 יחידות, כ-97 ₪ בחודש. הכנתי שתי פניות. כדאי לשלוח את שתיהן עכשיו, במקביל.",
            "cards": [
                {"type": "result", "ok": False, "expected": 26.75, "paid": 22.75, "gap_units": 4, "gap_ils": 97.08,
                 "pay_month": "2026-09",
                 "parts": [{"units": 3, "label": "השתלמויות שנת תשפ\"ה (שבתון)", "rule_id": "R12_SABBATICAL_EXEMPT",
                            "source": "הסכם עוז לתמורה, סעיף 18(ה)", "quote": None},
                           {"units": 1, "label": "השתלמויות שנת תשפ\"ו", "rule_id": "R11_ANNUAL_CAP",
                            "source": "הסכם עוז לתמורה, סעיף 18(ד)", "quote": None}],
                 "slip_url": "", "how_url": ""},
                {"type": "action", "track": "payroll", "to": "חשבות השכר", "title": "פנייה לחשבות השכר",
                 "channel": "פורטל עובדי הוראה · פניות מקוונות", "link": "https://poh.education.gov.il",
                 "subject": "גמול השתלמות עוז לתמורה לא עודכן בתלוש ספטמבר 2026",
                 "body": f"שלום רב,\nשמי שרה כהן, ת\"ז {ID_PLACEHOLDER}.\n...", "attach": ["צילום התלוש"], "todo_url": ""},
                {"type": "action", "track": "institution", "to": "[מוסד ההשתלמות]", "title": "מייל למוסד ההשתלמות",
                 "channel": "מייל למזכירות המוסד", "link": None,
                 "subject": "בקשה לדיווח השתלמות", "body": f"שלום רב,\nשמי שרה כהן, ת\"ז {ID_PLACEHOLDER}.\n...",
                 "attach": ["תעודת סיום / אישור השתתפות בקורס"], "todo_url": ""},
            ],
            "case": case,
        }
    return {"status": "reply", "fallback": False, "did": [],
            "text": "שלום שרה. גררי לכאן את הקבצים שיש לך מהפורטל: התלוש, הקורסים שלי וההצטרפות לעוז לתמורה.",
            "cards": [], "case": dict(case, docs=[dict(d, status="missing", detail="") for d in case["docs"]], progress=10)}
