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

# --- 基本設定 ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
sys.stdout.reconfigure(encoding='utf-8')

# rclone.confはプロジェクトルートにある前提
RCLONE_CONFIG_PATH = str(Path(__file__).resolve().parent.parent / "rclone.conf")

# 環境変数
VAULT_PATH = Path(os.getenv("OBSIDIAN_VAULT_PATH", "/var/data/vault"))
PENDING_MEMOS_FILE = Path(os.getenv("PENDING_MEMOS_FILE", "/var/data/pending_memos.json"))
DROPBOX_REMOTE = os.getenv("DROPBOX_REMOTE", "dropbox")
REMOTE_DIR = os.getenv("DROPBOX_REMOTE_DIR", "vault")
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

VAULT_PATH.mkdir(parents=True, exist_ok=True)

# --- Dropbox同期 ---
def sync_with_dropbox():
    """rcloneを使用してDropboxと双方向同期を行う"""
    rclone_path = shutil.which("rclone")
    if not rclone_path:
        logging.error("[SYNC] rcloneが見つかりませんでした。")
        return False

    # --config オプションで設定ファイルの場所を明示的に指定する
    common_args = ["--config", RCLONE_CONFIG_PATH, "--update", "--create-empty-src-dirs", "--verbose"]
    try:
        cmd_down = [rclone_path, "copy", f"{DROPBOX_REMOTE}:{REMOTE_DIR}", str(VAULT_PATH)] + common_args
        logging.info(f"[SYNC] ダウンロードを開始します: {' '.join(cmd_down)}")
        res_down = subprocess.run(cmd_down, check=True, capture_output=True, text=True, encoding='utf-8')
        logging.info(f"[SYNC] ダウンロードが完了しました。\n{res_down.stdout}")

        cmd_up = [rclone_path, "copy", str(VAULT_PATH), f"{DROPBOX_REMOTE}:{REMOTE_DIR}"] + common_args
        logging.info(f"[SYNC] アップロードを開始します: {' '.join(cmd_up)}")
        res_up = subprocess.run(cmd_up, check=True, capture_output=True, text=True, encoding='utf-8')
        logging.info(f"[SYNC] アップロードが完了しました。\n{res_up.stdout}")

        logging.info("[SYNC] Dropboxとの同期に成功しました。")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"[SYNC] 同期に失敗しました (終了コード: {e.returncode})")
        logging.error(f"STDOUT: {e.stdout}")
        logging.error(f"STDERR: {e.stderr}")
        return False
    except Exception as e:
        logging.error(f"[SYNC] 不明なエラーが発生しました: {e}", exc_info=True)
        return False

def process_pending_memos():
    if not PENDING_MEMOS_FILE.exists(): return True
    lock = FileLock(str(PENDING_MEMOS_FILE) + ".lock")
    with lock:
        try:
            with open(PENDING_MEMOS_FILE, "r", encoding="utf-8") as f: memos = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError): memos = []
        if not memos: return True
        logging.info(f"[PROCESS] {len(memos)}件のメモを処理します...")
        memos_by_date = {}
        for memo in memos:
            try:
                timestamp_utc = datetime.fromisoformat(memo['created_at'].replace('Z', '+00:00'))
                timestamp_jst = timestamp_utc.astimezone(JST)
                date_str = timestamp_jst.strftime('%Y-%m-%d')
                if date_str not in memos_by_date: memos_by_date[date_str] = []
                memos_by_date[date_str].append(memo)
            except (KeyError, ValueError) as e:
                logging.warning(f"タイムスタンプ処理エラー: {e} - スキップ: {memo}")
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
                logging.error(f"[PROCESS] ファイル書き込みエラー ({file_path}): {e}", exc_info=True)
                return False
        try:
            PENDING_MEMOS_FILE.unlink()
            logging.info("[PROCESS] 処理済みメモファイルを削除しました。")
        except OSError as e:
            logging.error(f"[PROCESS] メモファイル削除に失敗: {e}")
            return False
    return True

def main():
    logging.info("--- 同期ワーカーを開始します ---")
    if not sync_with_dropbox():
        logging.critical("初回同期に失敗。処理を中断。")
        sys.exit(1)
    if not process_pending_memos():
        logging.critical("メモ処理に失敗。処理を中断。")
        sys.exit(1)
    if not sync_with_dropbox():
        logging.critical("最終同期に失敗。")
        sys.exit(1)
    logging.info("--- 同期ワーカー正常完了 ---")

if __name__ == "__main__":
    main()