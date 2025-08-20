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
_last_saved = {"author": None, "content": None, "ts": 0}


async def add_memo_async(author: str, content: str):
    """非同期でメモを保存する。二重保存を防止。"""
    global _last_saved
    now = time.time()

    # --- デバッグログ ---
    logging.info(f"[add_memo_async] called | author={author}, content={content}")

    # --- 直近の保存と同一ならスキップ（5秒以内）---
    if (
        _last_saved["author"] == author
        and _last_saved["content"] == content
        and now - _last_saved["ts"] < 5
    ):
        logging.warning("[add_memo_async] Duplicate detected (recent save), skipping.")
        return

    _last_saved = {"author": author, "content": content, "ts": now}

    memo = {
        "author": author,
        "content": content,
        "timestamp": datetime.utcnow().isoformat() + "Z"
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

        # --- ファイル末尾との重複チェック ---
        if memos and memos[-1]["author"] == memo["author"] and memos[-1]["content"] == memo["content"]:
            logging.warning("[add_memo_async] Duplicate detected (last entry), skipping save.")
            return

        memos.append(memo)

        tmp_file = f"{PENDING_MEMOS_FILE}.tmp"
        async with aiofiles.open(tmp_file, mode='w', encoding='utf-8') as f:
            await f.write(json.dumps(memos, ensure_ascii=False, indent=2))
        os.replace(tmp_file, PENDING_MEMOS_FILE)

        logging.info("[add_memo_async] Memo saved successfully.")