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

PENDING_MEMOS_FILE = Path(os.getenv("PENDING_MEMOS_FILE", "pending_memos.json"))
PENDING_MEMOS_FILE.parent.mkdir(parents=True, exist_ok=True)

# 同期的な書き込み処理（内部でのみ使用）
def _add_memo_sync(content, author, created_at):
    """ファイルへの書き込みを行う同期関数"""
    data = {
        "content": content,
        "author": author,
        "created_at": created_at,
    }
    lock = FileLock(str(PENDING_MEMOS_FILE) + ".lock")
    with lock:
        memos = []
        if PENDING_MEMOS_FILE.exists():
            try:
                # ファイルから既存のメモを読み込む
                with open(PENDING_MEMOS_FILE, "r", encoding="utf-8") as f:
                    memos = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                # ファイルが空または壊れている場合は空のリストから開始
                memos = []
        
        memos.append(data)
        
        # 安全な書き込み（一時ファイル経由）
        tmp_file = PENDING_MEMOS_FILE.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(memos, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, PENDING_MEMOS_FILE)

    logging.info(f"Memo saved: {content[:30]}...")

# Botから呼び出すための非同期版関数
async def add_memo_async(content, author="Unknown", created_at=None):
    """Botの非同期処理を妨げずにメモを保存する関数"""
    created_at = created_at or datetime.now(timezone.utc).isoformat()
    # 同期的なファイル書き込みを別スレッドで実行し、メイン処理をブロックしないようにする
    await asyncio.to_thread(_add_memo_sync, content, author, created_at)