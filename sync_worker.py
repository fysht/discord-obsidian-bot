import os
import json
import shutil
import asyncio
import logging
import pytz
import subprocess
from pathlib import Path
from datetime import datetime
from filelock import FileLock

# --- 基本設定 ---
# ログ設定は呼び出し元のBotに任せるため、ここでのbasicConfigは不要

VAULT_PATH = Path(os.getenv("OBSIDIAN_VAULT_PATH", "./vault"))
PENDING_MEMOS_FILE = Path(os.getenv("PENDING_MEMOS_FILE", "pending_memos.json"))
RCLONE_CONFIG_PATH = Path(os.getenv("RCLONE_CONFIG_PATH", "rclone.conf"))
DROPBOX_REMOTE = os.getenv("DROPBOX_REMOTE", "dropbox")

VAULT_PATH.mkdir(parents=True, exist_ok=True)

# --- Dropbox同期 ---
async def sync_with_dropbox():
    # この関数は変更なし
    if not shutil.which("rclone"):
        logging.error("rclone not found in PATH.")
        return

    try:
        subprocess.run([
            "rclone", "copy", str(VAULT_PATH), f"{DROPBOX_REMOTE}:/vault",
            "--config", str(RCLONE_CONFIG_PATH),
            "--update", "--create-empty-src-dirs", "--verbose"
        ], check=True, capture_output=True)
        subprocess.run([
            "rclone", "copy", f"{DROPBOX_REMOTE}:/vault", str(VAULT_PATH),
            "--config", str(RCLONE_CONFIG_PATH),
            "--update", "--create-empty-src-dirs", "--verbose"
        ], check=True, capture_output=True)
        logging.info("Dropbox sync successful.")
    except subprocess.CalledProcessError as e:
        logging.error(f"rclone sync failed: {e.stderr.decode('utf-8', errors='ignore')}")

# --- メモ処理 ---
async def process_pending_memos():
    lock = FileLock(str(PENDING_MEMOS_FILE) + ".lock")
    if not PENDING_MEMOS_FILE.exists():
        return

    with lock:
        with open(PENDING_MEMOS_FILE, "r", encoding="utf-8") as f:
            try: memos = json.load(f)
            except json.JSONDecodeError: memos = []
        
        if not memos:
            return

        logging.info(f"Processing {len(memos)} pending memos...")
        memos_by_date = {}
        jst = pytz.timezone('Asia/Tokyo')

        for memo in memos:
            timestamp_dt = datetime.fromisoformat(memo['created_at'])
            post_time_jst = timestamp_dt.astimezone(jst)
            post_date_str = post_time_jst.strftime('%Y-%m-%d')
            memos_by_date.setdefault(post_date_str, []).append(memo)
        
        for post_date, memos_in_date in memos_by_date.items():
            try:
                daily_file = VAULT_PATH / f"{post_date}.md"
                daily_file.parent.mkdir(parents=True, exist_ok=True)

                obsidian_content = []
                for memo in memos_in_date:
                    timestamp_dt = datetime.fromisoformat(memo['created_at'])
                    time_str = timestamp_dt.astimezone(jst).strftime('%H:%M')
                    content = memo['content'].replace('\n', '\n\t- ')
                    
                    formatted_memo = f"- {time_str}\n\t- {content}"
                    obsidian_content.append(formatted_memo)
                
                full_content = "\n" + "\n".join(obsidian_content)

                with open(daily_file, "a", encoding="utf-8") as f:
                    f.write(full_content)
                
                logging.info(f"Successfully processed {len(memos_in_date)} memos into {daily_file}")

            except Exception as e:
                logging.error(f"Failed to write memos for date {post_date}: {e}")

        PENDING_MEMOS_FILE.unlink()

# --- メイン処理 ---
async def main():
    """
    SyncCogから呼び出された際に、同期とメモ処理を「一度だけ」実行する。
    """
    # 処理の順序は ダウンロード -> ローカル処理 -> アップロード が安全
    await sync_with_dropbox() 
    await process_pending_memos()
    await sync_with_dropbox()

if __name__ == "__main__":
    # このファイルが直接実行された場合に備えて、非同期処理を実行
    asyncio.run(main())