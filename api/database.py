import aiosqlite
import datetime
import json
import logging
from pathlib import Path
from config import JST

DB_PATH = Path(__file__).parent.parent / "chat_history.db"

async def restore_db_from_drive(drive_service, drive_folder_id):
    """Google Driveからchat_history.dbをダウンロードして復元する"""
    try:
        service = drive_service.get_service()
        if not service: return
        
        bot_folder_id = await drive_service.find_file(service, drive_folder_id, ".bot")
        if not bot_folder_id: return
        
        file_id = await drive_service.find_file(service, bot_folder_id, "chat_history.db")
        if file_id:
            await drive_service.download_file(service, file_id, str(DB_PATH))
            logging.info("[Database] chat_history.dbをGoogle Driveから復元しました。")
    except Exception as e:
        logging.error(f"[Database] リストアに失敗しました: {e}")

async def backup_db_to_drive(drive_service, drive_folder_id):
    """現在のchat_history.dbをGoogle Driveへ同期・バックアップする"""
    if not DB_PATH.exists(): return
    try:
        service = drive_service.get_service()
        if not service: return
        
        bot_folder_id = await drive_service.find_file(service, drive_folder_id, ".bot")
        if not bot_folder_id:
            bot_folder_id = await drive_service.create_folder(service, drive_folder_id, ".bot")
            
        file_id = await drive_service.find_file(service, bot_folder_id, "chat_history.db")
        if file_id:
            await drive_service.update_file(service, file_id, str(DB_PATH))
        else:
            await drive_service.upload_file(service, bot_folder_id, "chat_history.db", str(DB_PATH), "application/octet-stream")
        logging.info("[Database] chat_history.dbをGoogle Driveにバックアップしました。")
    except Exception as e:
        logging.error(f"[Database] バックアップに失敗しました: {e}")


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
    now = datetime.datetime.now(JST).isoformat()
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
    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT role, content, timestamp FROM messages WHERE timestamp LIKE ? ORDER BY id",
            (f"{today}%",),
        )
        rows = await cursor.fetchall()
        lines = []
        for row in rows:
            prefix = "[私]" if row["role"] == "user" else "[マネージャー]"
            lines.append(f"{prefix} {row['content']}")
        return "\n".join(lines)

async def clear_history():
    """全会話履歴をリセット（削除）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("DELETE FROM messages")
        await db.commit()
