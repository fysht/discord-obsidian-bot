import json
import os
import asyncio
from datetime import datetime
import aiofiles
import logging

PENDING_MEMOS_FILE = "pending_memos.json"

# 排他制御用のロック
file_lock = asyncio.Lock()

async def add_memo_async(author: str, content: str):
    """非同期でメモを保存する。重複保存を防止。"""
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
                    memos = []

        # 重複チェック（直前の保存と同じならスキップ）
        if memos and memos[-1]["author"] == memo["author"] and memos[-1]["content"] == memo["content"]:
            logging.warning("Duplicate memo detected, skipping save.")
            return

        memos.append(memo)

        tmp_file = f"{PENDING_MEMOS_FILE}.tmp"
        async with aiofiles.open(tmp_file, mode='w', encoding='utf-8') as f:
            await f.write(json.dumps(memos, ensure_ascii=False, indent=2))
        os.replace(tmp_file, PENDING_MEMOS_FILE)