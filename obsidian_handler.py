import os
import json
import logging
import asyncio
from pathlib import Path
from filelock import FileLock
from datetime import datetime, timezone
from dotenv import load_dotenv

# .envファイルから設定を読み込む
load_dotenv()

PENDING_MEMOS_FILE = Path(os.getenv("PENDING_MEMOS_FILE", "/var/data/pending_memos.json"))
PENDING_MEMOS_FILE.parent.mkdir(parents=True, exist_ok=True)

def _add_memo_sync(content, author, created_at, message_id):
    """ファイルへの書き込みを行う同期関数"""
    data = {
        "id": str(message_id),
        "content": content,
        "author": author,
        "created_at": created_at,
    }
    lock = FileLock(str(PENDING_MEMOS_FILE) + ".lock")
    with lock:
        memos = []
        if PENDING_MEMOS_FILE.exists() and PENDING_MEMOS_FILE.stat().st_size > 0:
            try:
                with open(PENDING_MEMOS_FILE, "r", encoding="utf-8") as f:
                    memos = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                memos = []
        
        # 同じIDのメモが既に存在しないかチェック
        if not any(memo.get("id") == str(message_id) for memo in memos):
            memos.append(data)
            tmp_file = PENDING_MEMOS_FILE.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(memos, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, PENDING_MEMOS_FILE)
            logging.info(f"[obsidian_handler] memo saved (message_id={message_id})")
        else:
            logging.warning(f"[obsidian_handler] Memo with ID {message_id} already exists. Skipping.")

async def add_memo_async(content, *, author="Unknown", created_at=None, message_id=None):
    """Botの非同期処理を妨げずにメモを保存する関数"""
    created_at = created_at or datetime.now(timezone.utc).isoformat()
    await asyncio.to_thread(_add_memo_sync, content, author, created_at, message_id)