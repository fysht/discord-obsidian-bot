import os
import sys
import json
import shutil
import logging
import zoneinfo
import subprocess
from pathlib import Path
from datetime import datetime
from filelock import FileLock

# --- 環境変数から設定内容を直接読み込み、ファイルを作成する ---
RCLONE_CONFIG_CONTENT = os.getenv("RCLONE_CONFIG_CONTENT")
# Renderの書き込み可能領域にパスを固定
RCLONE_CONFIG_PATH = "/var/data/rclone.conf" 

if RCLONE_CONFIG_CONTENT:
    try:
        Path(RCLONE_CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(RCLONE_CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(RCLONE_CONFIG_CONTENT)
        logging.info(f"環境変数からrclone.confを {RCLONE_CONFIG_PATH} に作成しました。")
    except Exception as e:
        logging.error(f"rclone.conf の作成に失敗しました: {e}", exc_info=True)
        sys.exit(1)
else:
    logging.critical("環境変数 'RCLONE_CONFIG_CONTENT' が設定されていません。ローカル実行の場合は.envファイルを確認してください。")
    sys.exit(1)
# ------------------------------------

# --- 基本設定 ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
sys.stdout.reconfigure(encoding='utf-8')

VAULT_PATH = Path(os.getenv("OBSIDIAN_VAULT_PATH", "/var/data/vault"))
PENDING_MEMOS_FILE = Path(os.getenv("PENDING_MEMOS_FILE", "/var/data/pending_memos.json"))
DROPBOX_REMOTE = os.getenv("DROPBOX_REMOTE", "dropbox")
REMOTE_DIR = os.getenv("DROPBOX_REMOTE_DIR", "vault")
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

VAULT_PATH.mkdir(parents=True, exist_ok=True)

def sync_with_dropbox():
    rclone_path = shutil.which("rclone")
    if not rclone_path:
        # Render環境では /usr/local/bin にあるはずなので、パスを明示的に指定してみる
        rclone_path_alt = "/usr/local/bin/rclone"
        if Path(rclone_path_alt).exists():
            rclone_path = rclone_path_alt
        else:
            logging.error("[SYNC] rcloneが見つかりませんでした。")
            return False

    common_args = ["--config", RCLONE_CONFIG_PATH, "--update", "--create-empty-src-dirs", "--verbose"]
    try:
        cmd_down = [rclone_path, "copy", f"{DROPBOX_REMOTE}:{REMOTE_DIR}", str(VAULT_PATH)] + common_args
        res_down = subprocess.run(cmd_down, check=True, capture_output=True, text=True, encoding='utf-8')
        logging.info(f"[SYNC] ダウンロード完了:\n{res_down.stdout}")
        cmd_up = [rclone_path, "copy", str(VAULT_PATH), f"{DROPBOX_REMOTE}:{REMOTE_DIR}"] + common_args
        res_up = subprocess.run(cmd_up, check=True, capture_output=True, text=True, encoding='utf-8')
        logging.info(f"[SYNC] アップロード完了:\n{res_up.stdout}")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"[SYNC] 同期失敗 (コード: {e.returncode})\nSTDERR:\n{e.stderr}")
        return False
    except Exception as e:
        logging.error(f"[SYNC] 不明なエラー: {e}", exc_info=True)
        return False

def process_pending_memos():
    if not PENDING_MEMOS_FILE.exists(): return True
    lock = FileLock(str(PENDING_MEMOS_FILE) + ".lock")
    with lock:
        try:
            with open(PENDING_MEMOS_FILE, "r", encoding="utf-8") as f: memos = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError): memos = []
        if not memos: return True
        logging.info(f"[PROCESS] {len(memos)}件のメモを処理...")
        memos_by_date = {}
        for memo in memos:
            try:
                timestamp_utc = datetime.fromisoformat(memo['created_at'].replace('Z', '+00:00'))
                date_str = timestamp_utc.astimezone(JST).strftime('%Y-%m-%d')
                if date_str not in memos_by_date: memos_by_date[date_str] = []
                memos_by_date[date_str].append(memo)
            except (KeyError, ValueError): pass
        daily_notes_path = VAULT_PATH / "DailyNotes"
        daily_notes_path.mkdir(parents=True, exist_ok=True)
        for date_str, memos_in_date in memos_by_date.items():
            try:
                file_path = daily_notes_path / f"{date_str}.md"
                lines_to_append = []
                for memo in sorted(memos_in_date, key=lambda m: m['created_at']):
                    time_str = datetime.fromisoformat(memo['created_at'].replace('Z', '+00:00')).astimezone(JST).strftime('%H:%M')
                    content_lines = memo['content'].strip().split('\n')
                    formatted_content = f"- {content_lines[0]}"
                    if len(content_lines) > 1:
                        formatted_content += "\n" + "\n".join([f"\t- {line}" for line in content_lines[1:]])
                    lines_to_append.append(f"- {time_str}\n\t{formatted_content}")
                with open(file_path, "a", encoding="utf-8") as f:
                    if f.tell() > 0: f.write("\n")
                    f.write("\n".join(lines_to_append))
            except Exception as e:
                logging.error(f"[PROCESS] ファイル書き込みエラー: {e}", exc_info=True)
                return False
        try:
            PENDING_MEMOS_FILE.unlink()
        except OSError: pass
    return True

def main():
    if not sync_with_dropbox(): sys.exit(1)
    if not process_pending_memos(): sys.exit(1)
    if not sync_with_dropbox(): sys.exit(1)
    logging.info("--- 同期ワーカー正常完了 ---")

if __name__ == "__main__":
    main()