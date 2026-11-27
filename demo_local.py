"""
מכין את מסד הדמו המקומי ומדפיס את הקישור לצ'אט. רץ מ-start-demo.bat.

    set DATABASE_URL=local
    python demo_local.py          # יוצר טבלאות + שרה כהן (T900) אם חסרים, ומדפיס קישור

המסד המקומי (pgserver) יושב ב-%LOCALAPPDATA%\\optipay-pg. המסד המשותף לא נוגע בו.
"""
import os
import subprocess
import sys

if os.environ.get("DATABASE_URL") != "local":
    sys.exit("הריצי עם DATABASE_URL=local (ראו start-demo.bat)")

from db import get_connection  # noqa: E402
from engine import init_db  # noqa: E402

init_db(reset=False)
conn = get_connection()
row = conn.execute("SELECT access_token FROM teachers WHERE teacher_id='T900'").fetchone()
conn.close()
if not row:
    subprocess.run([sys.executable, "demo_seed.py"], check=True)
    conn = get_connection()
    row = conn.execute("SELECT access_token FROM teachers WHERE teacher_id='T900'").fetchone()
    conn.close()
# מורת הדמו היא T900, "טובה כנען", כמו במסד המשותף (ראו .claude/skills/optipay-demo)
conn = get_connection()
conn.execute("UPDATE teachers SET full_name=%s WHERE teacher_id='T900'", ("טובה כנען",))
conn.commit()
conn.close()
port = os.environ.get("PORT", "5050")
print(f"http://127.0.0.1:{port}/my/{row['access_token']}/chat")
