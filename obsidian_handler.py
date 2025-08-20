import json
import os
import asyncio
import time
from datetime import datetime
import aiofiles
import logging

PENDING_MEMOS_FILE = "pending_memos.json"

# 排他制御用のロック
file_lock = asyncio.Lock()

# 直近保存した内容を記録（短時間の重複を防ぐ）
_last_saved = {"id": None, "ts": 0}


async def add_memo_async(author: str, content: str, message_id: str):
    """非同期でメモを保存する。二重保存を防止。"""
    global _last_saved
    now = time.time()

    logging.info(f"[add_memo_async] called | author={author}, content={content}, id={message_id}")

    # --- 直近の保存と同一IDならスキップ（5秒以内）---
    if (
        _last_saved["id"] == message_id
        and now - _last_saved["ts"] < 5
    ):
        logging.warning(f"[add_memo_async] Duplicate detected (recent save, id={message_id}), skipping.")
        return

    _last_saved = {"id": message_id, "ts": now}

    memo = {
        "id": message_id,
        "author": author,
        "content": content,
        "created_at": datetime.utcnow().isoformat() + "Z"
    }

    async with file_lock:  # 複数同時書き込み防止
        if not os.path.exists(PENDING_MEMOS_FILE):
            memos = []
        else:
            async with aiofiles.open(PENDING_MEMOS_FILE, mode='r', encoding='utf-8') as f:
                content_existing = await f.read()
                try:
                    memos = json.loads(content_existing)
                except json.JSONDecodeError:
                    logging.error("[add_memo_async] JSON decode error, resetting memos.")
                    memos = []

        # --- ID重複チェック ---
        if any(m.get("id") == message_id for m in memos):
            logging.warning(f"[add_memo_async] Duplicate detected in file (id={message_id}), skipping save.")
            return

        memos.append(memo)

        tmp_file = f"{PENDING_MEMOS_FILE}.tmp"
        async with aiofiles.open(tmp_file, mode='w', encoding='utf-8') as f:
            await f.write(json.dumps(memos, ensure_ascii=False, indent=2))
        os.replace(tmp_file, PENDING_MEMOS_FILE)

        logging.info(f"[add_memo_async] Memo saved successfully. id={message_id}")