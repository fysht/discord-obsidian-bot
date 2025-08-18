import os
import sys
import json
import shutil
import logging
import zoneinfo
import subprocess
import base64
from pathlib import Path
from datetime import datetime
from filelock import FileLock

# --- 設定ファイルのデコード処理 ---
# Render環境でBase64エンコードされた設定をファイルに書き戻す
RCLONE_CONFIG_BASE64 = os.getenv("RCLONE_CONFIG_BASE64")
RCLONE_CONFIG_PATH = os.getenv("RCLONE_CONFIG")

if RCLONE_CONFIG_BASE64 and RCLONE_CONFIG_PATH:
    try:
        config_dir = Path(RCLONE_CONFIG_PATH).parent
        config_dir.mkdir(parents=True, exist_ok=True)
        decoded_config = base64.b64decode(RCLONE_CONFIG_BASE64).decode("utf-8")
        with open(RCLONE_CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(decoded_config)
        logging.info(f"rclone.conf を {RCLONE_CONFIG_PATH} に復元しました。")
    except Exception as e:
        logging.error(f"rclone.conf の復元に失敗しました: {e}", exc_info=True)
# ------------------------------------

# --- 基本設定 ---
# ログ設定
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
# 標準出力のエンコーディングをUTF-8に設定
sys.stdout.reconfigure(encoding='utf-8')

# 環境変数から設定を読み込む
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

    common_args = ["--update", "--create-empty-src-dirs", "--verbose"]
    try:
        # 1. Download (remote -> local): 先にリモートの変更をローカルに反映
        cmd_down = [rclone_path, "copy", f"{DROPBOX_REMOTE}:{REMOTE_DIR}", str(VAULT_PATH)] + common_args
        logging.info(f"[SYNC] ダウンロードを開始します: {' '.join(cmd_down)}")
        res_down = subprocess.run(cmd_down, check=True, capture_output=True, text=True, encoding='utf-8')
        logging.info(f"[SYNC] ダウンロードが完了しました。\n{res_down.stdout}")

        # 2. Upload (local -> remote): ローカルの変更をリモートに反映
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

# --- メモ処理 ---
def process_pending_memos():
    """保留中のメモをObsidianのデイリーノートに書き込む"""
    if not PENDING_MEMOS_FILE.exists():
        logging.info("[PROCESS] 処理対象のメモファイルが見つかりません。")
        return True

    lock = FileLock(str(PENDING_MEMOS_FILE) + ".lock")
    with lock:
        try:
            with open(PENDING_MEMOS_FILE, "r", encoding="utf-8") as f:
                memos = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            memos = []

        if not memos:
            logging.info("[PROCESS] 処理対象のメモはありません。")
            return True

        logging.info(f"[PROCESS] {len(memos)}件のメモを処理します...")
        memos_by_date = {}
        for memo in memos:
            try:
                timestamp_utc = datetime.fromisoformat(memo['created_at'].replace('Z', '+00:00'))
                timestamp_jst = timestamp_utc.astimezone(JST)
                date_str = timestamp_jst.strftime('%Y-%m-%d')
                if date_str not in memos_by_date:
                    memos_by_date[date_str] = []
                memos_by_date[date_str].append(memo)
            except (KeyError, ValueError) as e:
                logging.warning(f"メモのタイムスタンプ処理中にエラー: {e} - スキップします: {memo}")

        daily_notes_path = VAULT_PATH / "DailyNotes"
        daily_notes_path.mkdir(parents=True, exist_ok=True)

        for date_str, memos_in_date in memos_by_date.items():
            try:
                file_path = daily_notes_path / f"{date_str}.md"
                lines_to_append = []
                for memo in sorted(memos_in_date, key=lambda m: m['created_at']):
                    timestamp_utc = datetime.fromisoformat(memo['created_at'].replace('Z', '+00:00'))
                    time_str = timestamp_utc.astimezone(JST).strftime('%H:%M')
                    content_lines = memo['content'].strip().split('\n')
                    # 1行目は通常のリストアイテム
                    formatted_content = f"- {content_lines[0]}"
                    # 2行目以降はインデントを付けたリストアイテム
                    if len(content_lines) > 1:
                        formatted_content += "\n" + "\n".join([f"\t- {line}" for line in content_lines[1:]])
                    lines_to_append.append(f"- {time_str}\n\t{formatted_content}")

                with open(file_path, "a", encoding="utf-8") as f:
                    if f.tell() > 0:
                        f.write("\n")
                    f.write("\n".join(lines_to_append))
                logging.info(f"[PROCESS] {len(memos_in_date)}件のメモを {file_path} に書き込みました。")
            except Exception as e:
                logging.error(f"[PROCESS] ファイル書き込み中にエラー ({file_path}): {e}", exc_info=True)
                return False

        # 処理が成功したので、保留メモファイルを削除
        try:
            PENDING_MEMOS_FILE.unlink()
            logging.info("[PROCESS] 処理済みのメモファイルを削除しました。")
        except OSError as e:
            logging.error(f"[PROCESS] メモファイルの削除に失敗しました: {e}")
            return False
    return True

# --- メイン処理 ---
def main():
    """メインの実行関数"""
    logging.info("--- 同期ワーカーを開始します ---")
    if not sync_with_dropbox():
        logging.critical("初回同期に失敗したため、処理を中断します。")
        sys.exit(1)

    if not process_pending_memos():
        logging.critical("メモの処理に失敗したため、処理を中断します。")
        sys.exit(1)

    if not sync_with_dropbox():
        logging.critical("最終同期に失敗しました。")
        sys.exit(1)

    logging.info("--- 同期ワーカーが正常に完了しました ---")

if __name__ == "__main__":
    main()