-- ============================================================
-- סכמת DB למנוע חישוב שכר - עוז לתמורה / גמול השתלמות
-- ============================================================

-- טבלת מטא-דאטה של החוקים (המקור, התוקף, הסדר)
-- הלוגיקה בפועל של כל חוק ממומשת כפונקציית פייתון (engine.py),
-- מזוהה על-ידי rule_id. הטבלה הזו היא "ספר החוקים" - שכבת ניהול
-- ותיעוד, לא מנוע הרצה בפני עצמה.
CREATE TABLE IF NOT EXISTS rules (
    rule_id       TEXT PRIMARY KEY,      -- מזהה ייחודי, למשל 'R11_ANNUAL_CAP'
    topic         TEXT NOT NULL,          -- 'גמול_השתלמות'
    title         TEXT NOT NULL,          -- שם קצר בעברית
    description   TEXT NOT NULL,          -- תיאור מלא של החוק (הלוגיקה במילים)
    source_doc    TEXT NOT NULL,          -- 'הסכם_עוז_לתמורה_סעיף_18' / 'חוזר_מנכל_השתלמויות'
    source_ref    TEXT,                   -- מספר סעיף מדויק במקור
    priority      INTEGER NOT NULL,       -- סדר הרצה - מספר נמוך = מוקדם יותר
    applies_when  TEXT,                   -- תיאור תמציתי של תנאי ההפעלה
    valid_from    TEXT,                   -- תאריך תחילת תוקף (ISO), NULL = מאז ומתמיד
    valid_to      TEXT,                   -- תאריך סיום תוקף, NULL = עדיין בתוקף
    overrides     TEXT,                   -- rule_id של חוק שהחוק הזה גובר עליו (אם יש)
    active        INTEGER NOT NULL DEFAULT 1
);

-- פרמטרים מספריים של החוקים (התקרות/אחוזים/שעות עצמם) - נפרד מהלוגיקה
-- שקובעת איזה חוק חל על מי. הלוגיקה (התנאים) נשארת בקוד engine.py; הערכים
-- הסופיים (למשל 112 שעות ליחידה, 1.2% ליחידה) נשלפים מכאן לפי תאריך.
CREATE TABLE IF NOT EXISTS rule_parameters (
    param_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id       TEXT NOT NULL REFERENCES rules(rule_id),
    param_name    TEXT NOT NULL,
    param_value   REAL NOT NULL,
    valid_from    TEXT,                   -- תאריך תחילת תוקף (ISO), NULL = מאז ומתמיד
    valid_to      TEXT                    -- תאריך סיום תוקף, NULL = עדיין בתוקף
);

-- טבלת קלט: נתוני מורה (snapshot של הקלט לחישוב)
CREATE TABLE IF NOT EXISTS teachers (
    teacher_id        TEXT PRIMARY KEY,
    full_name         TEXT,
    is_active_teacher INTEGER,            -- עובד הוראה בפועל, מוסמך
    has_seniority_year INTEGER,           -- השלים שנת ותק אחת
    is_pensioner      INTEGER DEFAULT 0,
    in_reform         INTEGER DEFAULT 0,  -- מועסק בתנאי עוז לתמורה
    reform_start_year TEXT,               -- שנה"ל תחילת העסקה ברפורמה, למשל 'תשע"ב'
    dual_reform_ofek_hadash INTEGER DEFAULT 0,
    U_pre             REAL DEFAULT 0,     -- יחידות שהוכרו ערב הרפורמה
    H_pre             REAL DEFAULT 0,     -- שעות שנצברו ערב הרפורמה
    has_double_degree_gimul INTEGER DEFAULT 0,  -- כבר קיבל גמול כפל תואר בעבר
    has_beinayim_gimul       INTEGER DEFAULT 0  -- כבר קיבל גמול ביניים בעבר
);

-- טבלת קורסים/לימודים לכל מורה
CREATE TABLE IF NOT EXISTS courses (
    course_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    teacher_id      TEXT NOT NULL REFERENCES teachers(teacher_id),
    start_date      TEXT NOT NULL,        -- ISO date
    school_year     TEXT,                 -- 'תשפ"ד' וכו', לנוחות
    hours           REAL NOT NULL,
    source_type     TEXT NOT NULL,        -- 'academic' | 'school_based' | 'conference' | 'language' | 'other'
    graded          INTEGER DEFAULT 0,    -- 1 = עם ציון
    grade           REAL,                 -- הציון שהתקבל (אם עם ציון)
    pre_approved    INTEGER DEFAULT 1,    -- קיבל אישור מפקח מראש (כשנדרש)
    is_advanced_degree_track INTEGER DEFAULT 0
);

-- טבלת סטטוס תעסוקתי שנתי (לצורך פטור משבתון/חצי-שבתון/חל"ת)
CREATE TABLE IF NOT EXISTS employment_status_by_year (
    teacher_id   TEXT NOT NULL REFERENCES teachers(teacher_id),
    school_year  TEXT NOT NULL,
    status       TEXT NOT NULL,           -- 'רגיל' | 'שבתון' | 'חצי-שבתון' | 'חלת'
    PRIMARY KEY (teacher_id, school_year)
);

-- טבלת תוצאות חישוב (output) - נשמר audit trail מלא
CREATE TABLE IF NOT EXISTS calculation_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    teacher_id      TEXT NOT NULL REFERENCES teachers(teacher_id),
    run_timestamp   TEXT NOT NULL,
    calc_date       TEXT NOT NULL,        -- לאיזה תאריך חושב (קובע תקרה חלה)
    U_regular       REAL,
    U_dd_or_mid     REAL,
    U_total         REAL,
    pct_addition    REAL,
    U_cap_applied   INTEGER,
    result_json     TEXT NOT NULL         -- הפלט המלא + trace (JSON)
);
