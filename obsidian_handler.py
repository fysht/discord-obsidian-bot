import os
import json
import logging
import asyncio
from pathlib import Path
from filelock import FileLock
from datetime import datetime
from dotenv import load_dotenv

# --- 追加: config.py から設定をインポート ---
from config import JST, PENDING_MEMOS_FILE

# .envファイルから設定を読み込む
load_dotenv()

# PENDING_MEMOS_FILE は config.py からインポート済み
PENDING_MEMOS_FILE.parent.mkdir(parents=True, exist_ok=True)

# ▼ 削除した部分 ▼
# try:
#     from zoneinfo import ZoneInfo
#     JST = ZoneInfo("Asia/Tokyo")
# except ImportError:
#     JST = timezone(timedelta(hours=+9), 'JST')
# ▲ 削除した部分 ▲


def _add_memo_sync(content, author, created_at, message_id, context, category):
    """ファイルへの書き込みを行う同期関数"""
    data = {
        "id": str(message_id),
        "content": content,
        "author": author,
        "created_at": created_at,
        "context": context,
        "category": category,
    }
    lock_path = str(PENDING_MEMOS_FILE) + ".lock"
    lock = FileLock(lock_path, timeout=10)

    try:
        with lock:
            memos = []
            if PENDING_MEMOS_FILE.exists() and PENDING_MEMOS_FILE.stat().st_size > 0:
                try:
                    with open(PENDING_MEMOS_FILE, "r", encoding="utf-8") as f:
                        memos = json.load(f)
                except json.JSONDecodeError:
                    logging.warning(
                        f"[obsidian_handler] JSON decode error in {PENDING_MEMOS_FILE}. Starting fresh."
                    )

            # message_id の重複チェック
            if not any(memo.get("id") == str(message_id) for memo in memos):
                memos.append(data)
                tmp_file = PENDING_MEMOS_FILE.with_suffix(".tmp")
                try:
                    with open(tmp_file, "w", encoding="utf-8") as f:
                        json.dump(memos, f, ensure_ascii=False, indent=2)

                    os.replace(tmp_file, PENDING_MEMOS_FILE)  # Atomic replacement
                    logging.info(
                        f"[obsidian_handler] memo saved to JSON (message_id={message_id})"
                    )
                except Exception as e:
                    logging.error(
                        f"[obsidian_handler] Failed to save memo to JSON: {e}",
                        exc_info=True,
                    )
                    if tmp_file.exists():
                        try:
                            os.remove(tmp_file)
                        except OSError:
                            pass
            else:
                logging.warning(
                    f"[obsidian_handler] Memo with ID {message_id} already exists in JSON. Skipping."
                )
    except TimeoutError:
        logging.error(
            f"[obsidian_handler] Could not acquire lock for {lock_path}. Skipping save."
        )
    except Exception as e:
        logging.error(
            f"[obsidian_handler] Error during file lock or JSON processing: {e}",
            exc_info=True,
        )


async def add_memo_async(
    content,
    *,
    author="Unknown",
    created_at=None,
    message_id=None,
    context=None,
    category=None,
):
    """Botの非同期処理を妨げずにメモを保存する関数"""
    created_at_iso = created_at or datetime.now(JST).isoformat()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        _add_memo_sync,
        content,
        author,
        created_at_iso,
        message_id,
        context,
        category,
    )
