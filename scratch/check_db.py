import sqlite3
import os

db_path = "chat_history.db"
if not os.path.exists(db_path):
    print("DB not found")
else:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, added_at FROM stocked_links ORDER BY id DESC LIMIT 10")
    rows = cursor.fetchall()
    for row in rows:
        print(row)
    conn.close()
