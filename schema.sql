-- ============================================================
-- סכמת DB למנוע חישוב שכר - עוז לתמורה / גמול השתלמות
-- ============================================================

-- חוקות: קבוצת חוקים בעלת שם (למשל "חוקת גמולי השתלמות"). כל חוק שייך
-- לחוקה אחת. מנוהל מהבקאופיס (/constitutions).
CREATE TABLE IF NOT EXISTS constitutions (
    constitution_id SERIAL PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,  -- מזהה קבוע בקוד, למשל 'gimul_hishtalmut'
    name            TEXT NOT NULL,
    description     TEXT
);

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
    active        INTEGER NOT NULL DEFAULT 1,
    constitution_id INTEGER REFERENCES constitutions(constitution_id),
    action        TEXT,                   -- מה החוק עושה (קוד מ-rule_vocab.ACTIONS)
    result_value  TEXT,                   -- תוצאה לחוק בלי פרמטרים (למשל "יחידה 17")
    logic_seeded  INTEGER NOT NULL DEFAULT 0  -- 1 = התנאים/הפעולה כבר נזרעו או נערכו
);

-- תנאי "חל על" של כל חוק, שורה לכל תנאי (החוקה הטבלאית). מתעד את הלוגיקה
-- במבנה קבוע; המנוע עדיין מחשב לפי הקוד ולא קורא את הטבלה הזו.
CREATE TABLE IF NOT EXISTS rule_conditions (
    condition_id  SERIAL PRIMARY KEY,
    rule_id       TEXT NOT NULL REFERENCES rules(rule_id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,       -- סדר התנאים בחוק
    connector     TEXT NOT NULL DEFAULT 'AND',  -- AND / OR - מחבר לתנאי הקודם
    field         TEXT NOT NULL,          -- קוד שדה מ-rule_vocab.FIELDS
    operator      TEXT NOT NULL,          -- eq/ne/lt/le/gt/ge/in/set/unset
    value         TEXT,                   -- ערך להשוואה (ISO לתאריך, 1/0 לכן/לא, רשימה מופרדת בפסיקים)
    value_field   TEXT                    -- או: השוואה לשדה אחר במקום לערך קבוע
);

-- פרמטרים מספריים של החוקים (התקרות/אחוזים/שעות עצמם) - נפרד מהלוגיקה
-- שקובעת איזה חוק חל על מי. הלוגיקה (התנאים) נשארת בקוד engine.py; הערכים
-- הסופיים (למשל 112 שעות ליחידה, 1.2% ליחידה) נשלפים מכאן לפי תאריך.
CREATE TABLE IF NOT EXISTS rule_parameters (
    param_id      SERIAL PRIMARY KEY,
    rule_id       TEXT NOT NULL REFERENCES rules(rule_id),
    param_name    TEXT NOT NULL,
    param_value   REAL NOT NULL,
    valid_from    TEXT,                   -- תאריך תחילת תוקף (ISO), NULL = מאז ומתמיד
    valid_to      TEXT                    -- תאריך סיום תוקף, NULL = עדיין בתוקף
);

-- יומן שינויים בחוקות (מהבקאופיס): כל שמירה נרשמת עם המצב לפני/אחרי.
-- rule_id בלי FK - כדי שההיסטוריה תישאר גם אם חוק יימחק בעתיד.
CREATE TABLE IF NOT EXISTS constitution_change_log (
    change_id       SERIAL PRIMARY KEY,
    constitution_id INTEGER NOT NULL REFERENCES constitutions(constitution_id),
    rule_id         TEXT,
    entity          TEXT NOT NULL,        -- 'constitution' | 'rule' | 'parameter'
    action          TEXT NOT NULL,        -- 'insert' | 'update' | 'delete'
    before_json     TEXT,                 -- המצב לפני (NULL בהוספה)
    after_json      TEXT,                 -- המצב אחרי (NULL במחיקה)
    reason          TEXT,                 -- סיבה/הערה חופשית
    source          TEXT NOT NULL DEFAULT 'manual',  -- 'manual' | 'agreement' (החלת הסכם) | 'restore' (שחזור גרסה)
    changed_at      TEXT NOT NULL
);

-- טבלת קלט: נתוני מורה (snapshot של הקלט לחישוב)
CREATE TABLE IF NOT EXISTS teachers (
    teacher_id        TEXT PRIMARY KEY,
    full_name         TEXT,
    is_active_teacher INTEGER,            -- עובד הוראה בפועל, מוסמך
    has_seniority_year INTEGER,           -- השלים שנת ותק אחת
    is_pensioner      INTEGER DEFAULT 0,
    in_reform         INTEGER DEFAULT 0,  -- מועסק בתנאי עוז לתמורה
    reform_start_year TEXT,               -- שנה"ל תחילת העסקה ברפורמה, למשל 'תשע"ב' (נגזר אוטומטית מ-reform_start_date)
    reform_start_date TEXT,               -- תאריך ISO מדויק של ההצטרפות לעוז לתמורה (לתצוגה/הזנה; reform_start_year הוא הנגזרת שהמנוע קורא בפועל)
    certification_date TEXT,              -- תאריך הסמכה כעובד הוראה מוסמך (מידע בלבד, לא משפיע כרגע על החישוב)
    dual_reform_ofek_hadash INTEGER DEFAULT 0,
    u_pre             REAL DEFAULT 0,     -- יחידות שהוכרו ערב הרפורמה
    h_pre             REAL DEFAULT 0,     -- שעות שנצברו ערב הרפורמה
    has_double_degree_gimul INTEGER DEFAULT 0,  -- כבר קיבל גמול כפל תואר בעבר
    double_degree_start_date TEXT,              -- החל ממתי מוכר גמול כפל תואר (מידע בלבד)
    has_beinayim_gimul       INTEGER DEFAULT 0, -- כבר קיבל גמול ביניים בעבר (חלופי לכפל תואר)
    beinayim_start_date      TEXT,              -- החל ממתי מוכר גמול ביניים (מידע בלבד)
    has_additional_double_degree_gimul INTEGER DEFAULT 0, -- "כפל שני"/כפל תואר נוסף - רק בנוסף לכפל תואר
    additional_double_degree_start_date TEXT,   -- החל ממתי מוכר כפל תואר נוסף (מידע בלבד)
    access_token             TEXT UNIQUE        -- קישור אישי למורה לצפייה/הזנה עצמית, בלי login
);

-- טבלת קורסים/לימודים לכל מורה
CREATE TABLE IF NOT EXISTS courses (
    course_id       SERIAL PRIMARY KEY,
    teacher_id      TEXT NOT NULL REFERENCES teachers(teacher_id),
    start_date      TEXT NOT NULL,        -- ISO date
    end_date        TEXT,                 -- ISO date
    description     TEXT,                 -- תיאור/שם חופשי של הקורס
    school_year     TEXT,                 -- 'תשפ"ד' וכו', לנוחות
    hours           REAL NOT NULL,        -- שעות שנספרות לחישוב (מעמודת "עוז לתמורה" בקובץ / סכום ידני)
    hours_graded    REAL,                 -- היקף שעות עם ציון (מהקובץ / הזנה ידנית)
    hours_ungraded  REAL,                 -- היקף שעות בלי ציון
    source_type     TEXT NOT NULL,        -- 'academic' | 'school_based' | 'conference' | 'language' | 'other'
    graded          INTEGER DEFAULT 0,    -- 1 = עם ציון
    grade           REAL,                 -- הציון שהתקבל (אם עם ציון)
    pre_approved    INTEGER DEFAULT 1,    -- קיבל אישור מפקח מראש (כשנדרש)
    is_advanced_degree_track INTEGER DEFAULT 0,
    counts_for_oz   INTEGER DEFAULT 1,    -- 0 = בקובץ אין ערך בעמודת "משתתף בגמולי עוז לתמורה" ("--"); נספר רק בטרום הרפורמה
    source          TEXT NOT NULL DEFAULT 'portal'  -- 'portal' = נקלט מקובץ "הקורסים שלי" | 'manual' = הוזן ידנית (לא בהכרח מדווח למשרד)
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
    run_id          SERIAL PRIMARY KEY,
    teacher_id      TEXT NOT NULL REFERENCES teachers(teacher_id),
    run_timestamp   TEXT NOT NULL,
    calc_date       TEXT NOT NULL,        -- לאיזה תאריך חושב (קובע תקרה חלה)
    u_regular       REAL,
    u_dd_or_mid     REAL,
    u_total         REAL,
    pct_addition    REAL,
    u_cap_applied   INTEGER,
    result_json     TEXT NOT NULL         -- הפלט המלא + trace (JSON)
);

-- קבצי PDF שהמורה/המנהל טענו (מכתב העסקה, הצטרפות לעוז לתמורה, הקורסים שלי).
-- נשמרים ב-DB ולא בדיסק, כי הדיסק של השרת (Render) נמחק בכל הפעלה מחדש.
CREATE TABLE IF NOT EXISTS teacher_documents (
    doc_id        SERIAL PRIMARY KEY,
    teacher_id    TEXT NOT NULL REFERENCES teachers(teacher_id),
    doc_type      TEXT NOT NULL,          -- 'maktav' | 'oz_join' | 'courses' ('heikef' בקבצים ישנים)
    filename      TEXT NOT NULL,
    content       BYTEA NOT NULL,
    uploaded_at   TEXT NOT NULL
);

-- תלושי שכר שהמורה טענה (או שהוזנו ידנית), עם שורת גמול ההשתלמות של עוז
-- לתמורה כפי שאושרה. משמשים להשוואה מול החישוב (מסך "התלוש שלי").
-- הקובץ עצמו נשמר ב-teacher_documents (doc_type='payslip').
CREATE TABLE IF NOT EXISTS payslips (
    payslip_id      SERIAL PRIMARY KEY,
    teacher_id      TEXT NOT NULL REFERENCES teachers(teacher_id),
    pay_month       TEXT NOT NULL,          -- 'YYYY-MM'
    gimul_code      TEXT,                   -- סמל השורה בתלוש (בדרך כלל 249)
    gimul_desc      TEXT,                   -- התיאור בתלוש, למשל 'ג. השת. עוז לתמורה'
    gimul_units     REAL,                   -- יחידות הגמול בתלוש (עמודת אחוז/כמות); NULL = השורה לא מופיעה
    gimul_amount    REAL,                   -- הסכום החודשי בש"ח
    doc_id          INTEGER,                -- הקובץ ב-teacher_documents (אם נטען)
    extraction_json TEXT,                   -- מה ש-Claude חילץ מהקובץ, לתיעוד
    run_id          INTEGER,                -- הרצת החישוב שמולה הושווה התלוש
    created_at      TEXT NOT NULL
);

-- הסכמים שנטענו (PDF/Word). Claude קורא את ההסכם ומציע שינויים בחוקות;
-- שום שינוי לא נכנס לחוקה לפני אישור ידני בדף ההסכם.
CREATE TABLE IF NOT EXISTS agreements (
    agreement_id  SERIAL PRIMARY KEY,
    title         TEXT,                   -- שם ההסכם (מה-AI, או שם הקובץ)
    filename      TEXT NOT NULL,
    content       BYTEA NOT NULL,         -- הקובץ המקורי
    uploaded_at   TEXT NOT NULL,
    status        TEXT NOT NULL,          -- 'analyzing' | 'ready' | 'failed' | 'applied' | 'rejected'
    summary       TEXT,                   -- תקציר ההסכם (מה-AI)
    proposal_json TEXT,                   -- השינויים המוצעים (מה-AI), JSON
    applied_json  TEXT,                   -- מה הוחל בפועל ומתי, JSON
    error         TEXT,                   -- הודעת שגיאה אם הניתוח נכשל
    applied_at    TEXT
);

-- גרסאות חוקה: תמונת מצב מלאה (חוקים, תנאים, פרמטרים) שנשמרת לפני כל החלת
-- הסכם ולפני כל שחזור - כדי שאפשר יהיה לחזור אליה.
CREATE TABLE IF NOT EXISTS constitution_versions (
    version_id      SERIAL PRIMARY KEY,
    constitution_id INTEGER NOT NULL REFERENCES constitutions(constitution_id),
    created_at      TEXT NOT NULL,
    label           TEXT NOT NULL,        -- למשל "לפני החלת הסכם: ..."
    agreement_id    INTEGER REFERENCES agreements(agreement_id),
    snapshot_json   TEXT NOT NULL
);

-- ============================================================
-- סוכן הליווי (agent/): התיק של כל מורה והיומן של מה שהסוכן עשה
-- ============================================================

-- תיק מורה אחד: מה ידוע ומאיזה מקור, השיחה עם Claude, ושאלה שממתינה לתשובה.
-- נשמר כדי שאפשר יהיה לעצור ולחזור בדיוק לאותה נקודה דרך הקישור האישי.
CREATE TABLE IF NOT EXISTS agent_cases (
    teacher_id     TEXT PRIMARY KEY REFERENCES teachers(teacher_id),
    case_json      TEXT NOT NULL,          -- התיק: קבצים, עובדות עם מקור, תוצאה, פניות, שאלה ממתינה
    messages_json  TEXT NOT NULL,          -- ההיסטוריה של השיחה עם Claude (כולל tool_use / tool_result)
    updated_at     TEXT NOT NULL
);

-- כל קריאה לכלי ומה שחזר ממנה: למסך "מה הסוכן עשה" ולבדיקה אחרי הדמו.
CREATE TABLE IF NOT EXISTS agent_events (
    event_id     SERIAL PRIMARY KEY,
    teacher_id   TEXT NOT NULL,
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL,            -- 'event' (מהמורה) | 'tool' | 'reply' | 'fallback' | 'error'
    name         TEXT,                     -- שם הכלי / סוג האירוע
    input_json   TEXT,
    output_json  TEXT
);
