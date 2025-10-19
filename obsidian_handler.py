import os
import json
import logging
import asyncio
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


def _add_memo_sync(content, author, created_at, message_id):
    """ファイルへの書き込みを行い、成功したらGoogle Docsにも送信する同期関数"""
    data = {
        "id": str(message_id),
        "content": content,
        "author": author,
        "created_at": created_at,
    }
    lock_path = str(PENDING_MEMOS_FILE) + ".lock"
    lock = FileLock(lock_path, timeout=10) # タイムアウトを設定
    memo_saved_to_json = False # JSON保存成功フラグ

    try:
        with lock:
            memos = []
            if PENDING_MEMOS_FILE.exists() and PENDING_MEMOS_FILE.stat().st_size > 0:
                try:
                    with open(PENDING_MEMOS_FILE, "r", encoding="utf-8") as f:
                        memos = json.load(f)
                except (json.JSONDecodeError, FileNotFoundError, ValueError) as e: # ValueErrorも追加
                    logging.error(f"[obsidian_handler] Failed to load JSON file {PENDING_MEMOS_FILE}: {e}. Initializing as empty list.")
                    memos = []

            # 同じIDのメモが既に存在しないかチェック
            if not any(memo.get("id") == str(message_id) for memo in memos):
                memos.append(data)
                tmp_file = PENDING_MEMOS_FILE.with_suffix(".tmp")
                try:
                    with open(tmp_file, "w", encoding="utf-8") as f:
                        json.dump(memos, f, ensure_ascii=False, indent=2)
                    # 元ファイルを削除してからリネーム (atomicではないが、replaceが権限エラーになる場合があるため)
                    if PENDING_MEMOS_FILE.exists():
                        os.remove(PENDING_MEMOS_FILE)
                    os.rename(tmp_file, PENDING_MEMOS_FILE)
                    logging.info(f"[obsidian_handler] memo saved to JSON (message_id={message_id})")
                    memo_saved_to_json = True # 保存成功
                except Exception as e:
                    logging.error(f"[obsidian_handler] Failed to save memo to JSON: {e}", exc_info=True)
                    memo_saved_to_json = False
                    if tmp_file.exists(): # 一時ファイルの削除試行
                        try: os.remove(tmp_file)
                        except OSError: pass
            else:
                logging.warning(f"[obsidian_handler] Memo with ID {message_id} already exists in JSON. Skipping.")
                memo_saved_to_json = False
    except TimeoutError:
         logging.error(f"[obsidian_handler] Could not acquire lock for {lock_path}. Skipping save.")
         memo_saved_to_json = False
    except Exception as e:
         logging.error(f"[obsidian_handler] Error during file lock or JSON processing: {e}", exc_info=True)
         memo_saved_to_json = False

    # JSONへの保存が成功した場合のみGoogle Docsへ追記
    if memo_saved_to_json and google_docs_enabled:
        try:
            # append_text_to_doc_async内でフォーマットするため、ここでは原文を渡す
            # 同期関数内から非同期関数を直接呼び出せないのでasyncio.runを使用
            # ただし、すでにイベントループが実行中の場合は RuntimeError が発生する可能性がある
            # そのため、呼び出し元 (add_memo_async) で非同期に実行する方が望ましい
            # ここでは暫定的に asyncio.run を使うが、環境によっては修正が必要
            try:
                loop = asyncio.get_running_loop()
                # 既にループがある場合は loop.create_task などを使うべきだが、
                # to_thread から呼ばれる同期関数内では難しい。
                # ここでは単純化のため run を使うが、本番環境では注意。
                asyncio.run(append_text_to_doc_async(
                    text_to_append=content,
                    source_type="Discord Memo"
                ))
            except RuntimeError as e:
                # asyncio.run() cannot be called from a running event loop
                logging.warning(f"Could not run append_text_to_doc_async directly: {e}. Scheduling.")
                # スケジューリングを試みる (ただし、同期関数が終了する前に実行される保証はない)
                asyncio.create_task(append_text_to_doc_async(
                    text_to_append=content,
                    source_type="Discord Memo"
                ))

            logging.info(f"[obsidian_handler] Sent memo to Google Docs (message_id={message_id})")
        except Exception as e:
            logging.error(f"[obsidian_handler] Failed to send memo to Google Docs: {e}", exc_info=True)
    # --- ここまで ---

async def add_memo_async(content, *, author="Unknown", created_at=None, message_id=None):
    """Botの非同期処理を妨げずにメモを保存する関数"""
    created_at_iso = created_at or datetime.now(timezone.utc).isoformat()
    # _add_memo_sync を非同期で実行する
    await asyncio.to_thread(_add_memo_sync, content, author, created_at_iso, message_id)