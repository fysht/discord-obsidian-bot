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

# ログ設定
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# --- 設定 ---
VAULT_PATH = Path(os.getenv("OBSIDIAN_VAULT_PATH", "/var/data/vault"))  # ✅ ローカルパス推奨
PENDING_MEMOS_FILE = Path(os.getenv("PENDING_MEMOS_FILE", "/var/data/pending_memos.json"))
DROPBOX_REMOTE = os.getenv("DROPBOX_REMOTE", "dropbox")
REMOTE_DIR = os.getenv("DROPBOX_REMOTE_DIR", "vault")  # デフォルトは dropbox:/vault

VAULT_PATH.mkdir(parents=True, exist_ok=True)

# --- Dropbox同期（rcloneを使用） ---
async def sync_with_dropbox():
    rclone_path = shutil.which("rclone")
    logging.info(f"[SYNC] rclone: {rclone_path}")
    logging.info(f"[SYNC] PATH={os.getenv('PATH')}")
    logging.info(f"[SYNC] RCLONE_CONFIG={os.getenv('RCLONE_CONFIG')}")

    if not rclone_path:
        logging.error("[SYNC] rclone not found in PATH.")
        return False

    # download (remote -> local)
    cmd_down = [
        "rclone", "copy", f"{DROPBOX_REMOTE}:/{REMOTE_DIR}", str(VAULT_PATH),
        "--update", "--create-empty-src-dirs", "--verbose"
    ]
    # upload (local -> remote)
    cmd_up = [
        "rclone", "copy", str(VAULT_PATH), f"{DROPBOX_REMOTE}:/{REMOTE_DIR}",
        "--update", "--create-empty-src-dirs", "--verbose"
    ]

    try:
        res_down = subprocess.run(cmd_down, check=True, capture_output=True)
        logging.info(f"[SYNC] download ok:\n{res_down.stdout.decode('utf-8', 'ignore')}")
    except subprocess.CalledProcessError as e:
        logging.error(f"[SYNC] download failed (code={e.returncode}):\n{e.stderr.decode('utf-8','ignore')}")
        return False

    try:
        res_up = subprocess.run(cmd_up, check=True, capture_output=True)
        logging.info(f"[SYNC] upload ok:\n{res_up.stdout.decode('utf-8', 'ignore')}")
    except subprocess.CalledProcessError as e:
        logging.error(f"[SYNC] upload failed (code={e.returncode}):\n{e.stderr.decode('utf-8','ignore')}")
        return False

    logging.info("[SYNC] Dropbox sync successful.")
    return True

# --- メモ処理 ---
async def process_pending_memos():
    if not PENDING_MEMOS_FILE.exists():
        logging.info(f"[PROCESS] pending file not found: {PENDING_MEMOS_FILE}")
        return

    lock = FileLock(str(PENDING_MEMOS_FILE) + ".lock")
    with lock:
        try:
            with open(PENDING_MEMOS_FILE, "r", encoding="utf-8") as f:
                memos = json.load(f)
        except json.JSONDecodeError:
            memos = []

        if not memos:
            logging.info("[PROCESS] no memos to process.")
            return

        logging.info(f"[PROCESS] processing {len(memos)} memos...")
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

                lines = []
                for memo in memos_in_date:
                    timestamp_dt = datetime.fromisoformat(memo['created_at'])
                    time_str = timestamp_dt.astimezone(jst).strftime('%H:%M')
                    content = memo['content'].replace('\n', '\n\t- ')
                    lines.append(f"- {time_str}\n\t- {content}")

                with open(daily_file, "a", encoding="utf-8") as f:
                    f.write("\n" + "\n".join(lines))

                logging.info(f"[PROCESS] wrote {len(memos_in_date)} memos -> {daily_file}")
            except Exception as e:
                logging.error(f"[PROCESS] failed to write for date {post_date}: {e}")

        # 処理済みなので削除
        PENDING_MEMOS_FILE.unlink(missing_ok=True)
        logging.info("[PROCESS] cleared pending memos.")

# --- メイン ---
async def main():
    # 安全な順序: remote -> local ダウンロード → ローカル処理 → アップロード
    await sync_with_dropbox()
    await process_pending_memos()
    await sync_with_dropbox()

if __name__ == "__main__":
    asyncio.run(main())