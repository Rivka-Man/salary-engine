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

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "db.env"))

DATABASE_URL = os.environ["DATABASE_URL"]


class Connection:
    def __init__(self, raw_conn):
        self._conn = raw_conn

    def execute(self, sql, params=None):
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
        self._conn.close()


def get_connection():
    raw = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return Connection(raw)
