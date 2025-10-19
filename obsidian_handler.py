import os
import json
import logging
import asyncio # asyncio をインポート
from pathlib import Path
from filelock import FileLock
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# google_docs_handlerをインポート
try:
    from google_docs_handler import append_text_to_doc_async
    google_docs_enabled = True
except ImportError:
    logging.warning("google_docs_handler.pyが見つからないため、Google Docs連携は無効です。")
    google_docs_enabled = False

# .envファイルから設定を読み込む
load_dotenv()

PENDING_MEMOS_FILE = Path(os.getenv("PENDING_MEMOS_FILE", "/var/data/pending_memos.json")) 
PENDING_MEMOS_FILE.parent.mkdir(parents=True, exist_ok=True)

# JSTゾーン情報を取得 (なければUTC+9)
try:
    from zoneinfo import ZoneInfo
    JST = ZoneInfo("Asia/Tokyo")
except ImportError:
    JST = timezone(timedelta(hours=+9), 'JST')


# loop 引数を追加
def _add_memo_sync(content, author, created_at, message_id, context, category, loop):
    """ファイルへの書き込みを行い、成功したらGoogle Docsにも送信する同期関数"""
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
    memo_saved_to_json = False

    try:
        with lock:
            memos = []
            if PENDING_MEMOS_FILE.exists() and PENDING_MEMOS_FILE.stat().st_size > 0:
                try:
                    with open(PENDING_MEMOS_FILE, "r", encoding="utf-8") as f:
                        memos = json.load(f)
                except (json.JSONDecodeError, FileNotFoundError, ValueError) as e:
                    logging.error(f"[obsidian_handler] Failed to load JSON file {PENDING_MEMOS_FILE}: {e}. Initializing as empty list.")
                    memos = []

            if not any(memo.get("id") == str(message_id) for memo in memos):
                memos.append(data)
                tmp_file = PENDING_MEMOS_FILE.with_suffix(".tmp")
                try:
                    with open(tmp_file, "w", encoding="utf-8") as f:
                        json.dump(memos, f, ensure_ascii=False, indent=2)
                    # if PENDING_MEMOS_FILE.exists(): # renameが上書きするので不要
                    #     os.remove(PENDING_MEMOS_FILE)
                    os.rename(tmp_file, PENDING_MEMOS_FILE) # rename はアトミック操作 (多くのOSで)
                    logging.info(f"[obsidian_handler] memo saved to JSON (message_id={message_id})")
                    memo_saved_to_json = True
                except Exception as e:
                    logging.error(f"[obsidian_handler] Failed to save memo to JSON: {e}", exc_info=True)
                    memo_saved_to_json = False
                    if tmp_file.exists():
                        try: os.remove(tmp_file)
                        except OSError: pass
            else:
                logging.warning(f"[obsidian_handler] Memo with ID {message_id} already exists in JSON. Skipping.")
                memo_saved_to_json = False # JSONへの保存はスキップしたがGoogle Docsへは送るべきか？ 仕様による。ここでは送らない。
    except TimeoutError:
         logging.error(f"[obsidian_handler] Could not acquire lock for {lock_path}. Skipping save.")
         memo_saved_to_json = False
    except Exception as e:
         logging.error(f"[obsidian_handler] Error during file lock or JSON processing: {e}", exc_info=True)
         memo_saved_to_json = False

    # run_coroutine_threadsafe を使用
    if memo_saved_to_json and google_docs_enabled and loop:
        try:
            # メインスレッドのイベントループにコルーチンを投入
            future = asyncio.run_coroutine_threadsafe(
                append_text_to_doc_async(
                    text_to_append=content,
                    source_type="Discord Memo"
                ),
                loop # 引数で受け取ったループを使用
            )
            # 必要であれば future.result(timeout=...) で完了を待機できるが、
            # ここでは投入するだけで完了は待たない（エラーハンドリングは困難になる）
            logging.info(f"[obsidian_handler] Scheduled sending memo to Google Docs (message_id={message_id})")
        except Exception as e:
            # run_coroutine_threadsafe 自体の呼び出しエラー
            logging.error(f"[obsidian_handler] Failed to schedule send memo to Google Docs: {e}", exc_info=True)
    elif not loop:
         logging.error("[obsidian_handler] Event loop not provided to _add_memo_sync. Cannot send to Google Docs.")

# イベントループを取得して渡す
async def add_memo_async(content, *, author="Unknown", created_at=None, message_id=None, context=None, category=None):
    """Botの非同期処理を妨げずにメモを保存する関数"""
    created_at_iso = created_at or datetime.now(timezone.utc).isoformat()
    # 現在実行中のイベントループを取得
    loop = asyncio.get_running_loop()
    # _add_memo_sync に loop を渡す
    await asyncio.to_thread(_add_memo_sync, content, author, created_at_iso, message_id, context, category, loop)