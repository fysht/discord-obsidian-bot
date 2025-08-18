import os
import json
import logging
import asyncio
from pathlib import Path
from filelock import FileLock
from datetime import datetime, timezone

# .envファイルから設定を読み込む
from dotenv import load_dotenv
load_dotenv()

# 環境変数からパスを取得し、絶対パスであることをログに出力
file_path_str = os.getenv("PENDING_MEMOS_FILE", "pending_memos.json")
PENDING_MEMOS_FILE = Path(file_path_str).resolve() # 常に絶対パスとして扱う
logging.info(f"PENDING_MEMOS_FILE is set to: {PENDING_MEMOS_FILE}")

# ディレクトリ作成処理
try:
    PENDING_MEMOS_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.info(f"Ensured parent directory exists: {PENDING_MEMOS_FILE.parent}")
except Exception as e:
    logging.error(f"Failed to create parent directory for PENDING_MEMOS_FILE: {e}", exc_info=True)


# 同期的な書き込み処理（内部でのみ使用）
def _add_memo_sync(content, author, created_at):
    """ファイルへの書き込みを行う同期関数"""
    data = {
        "content": content,
        "author": author,
        "created_at": created_at,
    }
    
    logging.info(f"Attempting to write to {PENDING_MEMOS_FILE}...")
    lock = FileLock(str(PENDING_MEMOS_FILE) + ".lock")
    
    try:
        with lock:
            memos = []
            if PENDING_MEMOS_FILE.exists():
                logging.info(f"{PENDING_MEMOS_FILE} exists. Reading existing memos.")
                try:
                    with open(PENDING_MEMOS_FILE, "r", encoding="utf-8") as f:
                        content_in_file = f.read()
                        if content_in_file:
                            memos = json.loads(content_in_file)
                        else:
                            memos = []
                except json.JSONDecodeError:
                    logging.warning(f"{PENDING_MEMOS_FILE} was corrupted or empty. Starting with an empty list.")
                    memos = []
            else:
                logging.info(f"{PENDING_MEMOS_FILE} does not exist. Creating a new file.")

            memos.append(data)

            tmp_file = PENDING_MEMOS_FILE.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(memos, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, PENDING_MEMOS_FILE)

        logging.info(f"Memo successfully saved to {PENDING_MEMOS_FILE}: {content[:30]}...")

    except Exception as e:
        logging.error(f"Failed to write memo to {PENDING_MEMOS_FILE}: {e}", exc_info=True)


# Botから呼び出すための非同期版関数
async def add_memo_async(content, author="Unknown", created_at=None):
    """Botの非同期処理を妨げずにメモを保存する関数"""
    created_at = created_at or datetime.now(timezone.utc).isoformat()
    await asyncio.to_thread(_add_memo_sync, content, author, created_at)