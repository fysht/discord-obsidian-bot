import os
import sys
import json
import subprocess
import logging
from pathlib import Path
from datetime import datetime
import zoneinfo
from filelock import FileLock, Timeout

# --- ロギング設定 ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
# 標準出力のエンコーディングをUTF-8に設定
sys.stdout.reconfigure(encoding='utf-8')

# --- 設定 ---
# 環境変数から設定を読み込む
file_path_str = os.getenv("PENDING_MEMOS_FILE", "pending_memos.json")
PENDING_MEMOS_FILE = Path(file_path_str).resolve()
VAULT_PATH = os.getenv("OBSIDIAN_VAULT_PATH") 
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

def sync_memos():
    """保留中のメモをObsidianに同期するメイン関数"""
    logging.info("Starting sync process...")
    if not VAULT_PATH:
        logging.error("ERROR: 環境変数 'OBSIDIAN_VAULT_PATH' が設定されていません。")
        return

    lock = FileLock(str(PENDING_MEMOS_FILE) + ".lock")
    
    try:
        with lock.acquire(timeout=10):
            if not PENDING_MEMOS_FILE.exists() or PENDING_MEMOS_FILE.stat().st_size == 0:
                logging.info("INFO: 同期対象のメモはありません。")
                return

            with open(PENDING_MEMOS_FILE, "r", encoding="utf-8") as f:
                try:
                    content_in_file = f.read()
                    if not content_in_file:
                        logging.info("INFO: pending_memos.json is empty.")
                        return
                    memos = json.loads(content_in_file)
                except json.JSONDecodeError:
                    logging.error("ERROR: pending_memos.json の解析に失敗しました。")
                    return
            
            if not memos:
                logging.info("INFO: メモのリストが空です。同期をスキップします。")
                return

            # --- Markdownファイル形式に変換 ---
            today = datetime.now(JST).date()
            date_str = today.strftime('%Y-%m-%d')
            
            # メモを時系列でソート
            sorted_memos = sorted(memos, key=lambda x: x.get('created_at', ''))
            
            content_list = []
            for memo in sorted_memos:
                ts_utc = datetime.fromisoformat(memo['created_at'])
                ts_jst = ts_utc.astimezone(JST)
                time_str = ts_jst.strftime('%H:%M')
                # メモの各行にタイムスタンプを付与
                for line in memo['content'].splitlines():
                    content_list.append(f"- {time_str} {line}")

            # rcloneでObsidianに同期
            # 一時ファイルに書き出し
            temp_file_path = Path(f"/tmp/{date_str}.md") # Renderでは/tmpへの書き出しが確実
            
            # 追記処理: 既存のファイルを取得 -> 追記 -> アップロード
            # 1. 既存ファイルの内容をダウンロード
            existing_content = ""
            # 保存先パスを 'DailyNotes' フォルダ配下にする
            destination_path = f'{VAULT_PATH}/DailyNotes/{date_str}.md'
            
            try:
                download_cmd = ['rclone', 'cat', destination_path]
                logging.info(f"Attempting to download existing note: {' '.join(download_cmd)}")
                result = subprocess.run(download_cmd, capture_output=True, text=True, encoding='utf-8')
                if result.returncode == 0:
                    existing_content = result.stdout
                    logging.info("Successfully downloaded existing note.")
                else:
                    logging.info("No existing note found or failed to download. A new note will be created.")

            except Exception as e:
                logging.warning(f"Could not download existing file, maybe it doesn't exist yet. Error: {e}")

            # 2. 新しいメモと結合して書き込み
            final_content = existing_content + "\n" + "\n".join(content_list)
            temp_file_path.write_text(final_content.strip(), encoding="utf-8")

            # 3. rcloneでアップロード (movetoからcopytoに変更)
            upload_cmd = ['rclone', 'copyto', str(temp_file_path), destination_path]
            logging.info(f"INFO: rcloneコマンドを実行します: {' '.join(upload_cmd)}")
            result = subprocess.run(upload_cmd, capture_output=True, text=True, encoding='utf-8')

            if result.returncode == 0:
                logging.info("SUCCESS: Obsidianへの同期が完了しました。")
                logging.info(f"rclone output:\n{result.stdout}")
                
                # 同期が成功したらpending_memos.jsonをクリアする
                with open(PENDING_MEMOS_FILE, "w", encoding="utf-8") as f:
                    json.dump([], f)
                logging.info("INFO: pending_memos.json をクリアしました。")
            else:
                logging.error(f"ERROR: rcloneの実行に失敗しました。Return Code: {result.returncode}")
                logging.error(f"Stderr:\n{result.stderr}")

    except Timeout:
        logging.error("ERROR: ファイルロックの取得に失敗しました。他のプロセスが使用中です。")
    except Exception as e:
        logging.error(f"ERROR: 同期処理中に予期せぬエラーが発生しました: {e}", exc_info=True)

if __name__ == "__main__":
    sync_memos()