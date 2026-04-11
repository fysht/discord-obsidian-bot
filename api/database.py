import aiosqlite
import datetime
import json
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "chat_history.db"


async def init_db():
    """データベースとテーブルを初期化"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        await db.commit()


async def save_message(role: str, content: str):
    """メッセージを保存（role: 'user' or 'assistant'）"""
    now = datetime.datetime.now().isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
            (role, content, now),
        )
        await db.commit()


async def get_history(limit: int = 20):
    """直近の会話履歴を取得"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT role, content, timestamp FROM messages ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        # 古い順に並び替えて返す
        return [
            {"role": row["role"], "content": row["content"], "timestamp": row["timestamp"]}
            for row in reversed(rows)
        ]


async def get_todays_log():
    """今日の会話ログをテキスト形式で取得"""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT role, content, timestamp FROM messages WHERE timestamp LIKE ? ORDER BY id",
            (f"{today}%",),
        )
        rows = await cursor.fetchall()
        lines = []
        for row in rows:
            prefix = "[私]" if row["role"] == "user" else "[秘書]"
            lines.append(f"{prefix} {row['content']}")
        return "\n".join(lines)
