"""
חיבור משותף ל-PostgreSQL (Supabase), בשימוש ע"י engine.py, app.py, ו-example_run.py.

get_connection() מחזירה עטיפה סביב psycopg2.connection שמוסיפה מתודת
.execute(...) שמחזירה cursor (בסגנון sqlite3.Connection.execute), כדי
לא לשנות את כל מקומות הקריאה בקוד שכבר כתוב בסגנון
conn.execute(sql, params).fetchone()/.fetchall(). כל שורה מוחזרת
כ-dict (RealDictCursor) כך ש-row["col"] עובד בדיוק כמו קודם עם
sqlite3.Row.
"""
import os
import re
import threading
import time

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "db.env"))

DATABASE_URL = os.environ["DATABASE_URL"]

# DATABASE_URL=local: מסד Postgres מקומי לדמו (pip install pgserver), בתיקייה OPTIPAY_LOCAL_DB.
# בלי רשת באמצע, כל שלב בשיחה לוקח פחות משנייה. המסד המשותף לא נוגע בו. ראו start-demo.bat.
if DATABASE_URL == "local":
    import pgserver

    _LOCAL_DIR = os.environ.get("OPTIPAY_LOCAL_DB") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "optipay-pg")
    os.makedirs(_LOCAL_DIR, exist_ok=True)
    _LOCAL_SERVER = pgserver.get_server(_LOCAL_DIR, cleanup_mode=None)
    DATABASE_URL = _LOCAL_SERVER.get_uri()


_LOCK = threading.Lock()
_READS = {}  # (sql, params) -> (rows, time)
_READ_TTL = float(os.environ.get("DB_READ_CACHE_SECONDS", "20"))
_WRITE_TABLE = re.compile(r"(?is)^(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-z_][a-z0-9_]*)")


class _Cached:
    """מה ש-execute מחזיר לשאילתת קריאה: כמו cursor, מעל שורות שכבר נקראו."""

    def __init__(self, rows):
        self._rows = [dict(r) for r in rows]
        self._i = 0
        self.rowcount = len(rows)

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        self._i += 1
        return self._rows[self._i - 1]

    def fetchall(self):
        rest, self._i = self._rows[self._i:], len(self._rows)
        return rest

    def __iter__(self):
        return iter(self.fetchall())


class Connection:
    def __init__(self, raw_conn):
        self._conn = raw_conn

    def execute(self, sql, params=None):
        # המסד רחוק (כ-0.25 שנ' לכל סבב), ותור אחד של הסוכן קורא את אותן טבלאות כמה פעמים.
        # קריאה שכבר נעשתה בשניות האחרונות מוחזרת מהזיכרון. כתיבה לטבלה מוחקת מהזיכרון כל
        # קריאה שנוגעת בה. (כתיבות של אחרים במסד המשותף נראות אחרי _READ_TTL לכל היותר.)
        s = sql.lstrip()
        if s[:6].upper() == "SELECT" and _READ_TTL > 0:
            key = (sql, repr(params))
            with _LOCK:
                hit = _READS.get(key)
            if hit and time.monotonic() - hit[1] < _READ_TTL:
                return _Cached(hit[0])
            cur = self._conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
            with _LOCK:
                _READS[key] = ([dict(r) for r in rows], time.monotonic())
            return _Cached(rows)
        m = _WRITE_TABLE.match(s)
        with _LOCK:
            if m:
                table = m.group(1).lower()
                for k in [k for k in _READS if table in k[0].lower()]:
                    del _READS[k]
            else:
                _READS.clear()
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return cur

    def executescript(self, sql):
        cur = self._conn.cursor()
        cur.execute(sql)
        return cur

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        # מחזירים את החיבור למאגר במקום לסגור: פתיחת חיבור חדש ל-Supabase לוקחת כ-2 שניות
        raw, self._conn = self._conn, None
        if raw is None:
            return
        try:
            if not raw.closed:
                raw.rollback()
                with _LOCK:
                    if len(_IDLE) < _MAX_IDLE:
                        _IDLE.append((raw, time.monotonic()))
                        return
            raw.close()
        except Exception:
            try:
                raw.close()
            except Exception:
                pass


_IDLE, _MAX_IDLE = [], 8


def _new_raw():
    raw = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    # המסד המשותף רחוק (סינגפור, כ-0.25 שנ' לכל סבב). בלי autocommit כל טרנזקציה עולה
    # עוד שני סבבים (BEGIN ו-COMMIT). עם autocommit כל פקודה היא סבב אחד, ו-commit לא עולה כלום.
    raw.autocommit = os.environ.get("DB_AUTOCOMMIT", "1") == "1"
    return raw


def get_connection():
    while True:
        with _LOCK:
            raw, since = _IDLE.pop() if _IDLE else (None, 0)
        if raw is None:
            return Connection(_new_raw())
        if raw.closed:
            continue
        if time.monotonic() - since < 60:
            return Connection(raw)
        try:  # חיבור שנשמר במאגר ונפל בינתיים (השרת סגר אותו): לוקחים אחר
            raw.cursor().execute("SELECT 1")
            raw.rollback()
            return Connection(raw)
        except Exception:
            try:
                raw.close()
            except Exception:
                pass
