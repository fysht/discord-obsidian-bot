import os
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone
from filelock import FileLock

# PENDING_MEMOS_FILE を環境変数で指定できるように
PENDING_MEMOS_FILE = Path(os.getenv("PENDING_MEMOS_FILE", "/var/data/pending_memos.json"))
PENDING_MEMOS_FILE.parent.mkdir(parents=True, exist_ok=True)

# 同期・非同期の両方から呼ばれる可能性を考慮し、プロセス間は FileLock で守る
_file_lock = FileLock(str(PENDING_MEMOS_FILE) + ".lock")

# 直近保存（プロセス内）のノイズ抑制
_last_saved = {"message_id": None, "ts": 0.0}
_last_saved_lock = asyncio.Lock()


async def add_memo_async(
    author: str,
    content: str,
    *,
    message_id: int | str | None,
    created_at: str | None = None
) -> None:
    """
    メモを pending_memos.json に追記する。
    - message_id が与えられていれば、それをキーに重複排除（最優先）
    - created_at は ISO8601（UTC, 例: 2025-08-20T12:34:56+00:00）を推奨
    """

    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()

    # プロセス内での直近二重呼び出しを軽減（極短時間の多重呼び出し対策）
    async with _last_saved_lock:
        from time import time
        now = time()
        if _last_saved["message_id"] == str(message_id) and (now - _last_saved["ts"] < 3.0):
            logging.warning("[obsidian_handler] recent duplicate call skipped (same message_id within 3s)")
            return
        _last_saved["message_id"] = str(message_id)
        _last_saved["ts"] = now

    memo = {
        "author": author,
        "content": content,
        "created_at": created_at,
        "message_id": str(message_id) if message_id is not None else None,  # ← 統一
    }

    # プロセス間の排他
    with _file_lock:
        # 既存読み込み
        try:
            if PENDING_MEMOS_FILE.exists():
                with open(PENDING_MEMOS_FILE, "r", encoding="utf-8") as f:
                    memos = json.load(f)
                if not isinstance(memos, list):
                    logging.error("[obsidian_handler] pending_memos.json is not a list. Resetting.")
                    memos = []
            else:
                memos = []
        except json.JSONDecodeError:
            logging.error("[obsidian_handler] JSON decode error. Resetting memos.")
            memos = []

        # --- 重複排除ロジック ---
        # 1) message_id が入っている場合は、message_id で重複排除
        if memo["message_id"] is not None:
            if any(m.get("message_id") == memo["message_id"] for m in memos):
                logging.info(f"[obsidian_handler] duplicate detected by message_id={memo['message_id']}, skip")
                return
        else:
            # 2) フォールバック：message_id が無いケース（将来の互換性確保）
            # author + content + created_at が完全一致なら重複扱い
            if any(
                (m.get("message_id") is None) and
                (m.get("author") == memo["author"]) and
                (m.get("content") == memo["content"]) and
                (m.get("created_at") == memo["created_at"])
                for m in memos
            ):
                logging.info("[obsidian_handler] duplicate detected by triplet, skip")
                return

        # 追記
        memos.append(memo)

        # 原子的に書き換え
        tmp = PENDING_MEMOS_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(memos, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PENDING_MEMOS_FILE)

        logging.info(f"[obsidian_handler] memo saved (message_id={memo['message_id']})")