import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime
import zoneinfo
from filelock import FileLock
from dotenv import load_dotenv
import dropbox
from dropbox.exceptions import ApiError
from dropbox.files import WriteMode

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
sys.stdout.reconfigure(encoding='utf-8')

PENDING_MEMOS_FILE = Path(os.getenv("PENDING_MEMOS_FILE", "/var/data/pending_memos.json"))
LAST_PROCESSED_ID_FILE = Path(os.getenv("LAST_PROCESSED_ID_FILE", "/var/data/last_processed_id.txt"))
DROPBOX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN")
DROPBOX_VAULT_PATH = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault") 
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

def process_pending_memos():
    """保留メモをDropbox上のDailyNoteに追加する"""
    if not PENDING_MEMOS_FILE.exists():
        return True

    lock = FileLock(str(PENDING_MEMOS_FILE) + ".lock")
    with lock:
        try:
            with open(PENDING_MEMOS_FILE, "r", encoding="utf-8") as f:
                memos = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            memos = []
            
        if not memos:
            return True

        logging.info(f"[PROCESS] {len(memos)} 件のメモをDropboxに保存します...")
        
        try:
            dbx = dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)
            logging.info("[DROPBOX] Dropboxへの接続に成功しました。")
        except Exception as e:
            logging.error(f"[DROPBOX] Dropboxへの接続に失敗しました: {e}")
            return False

        sorted_memos = sorted(memos, key=lambda m: int(m['id']))
        memos_by_date = {}
        for memo in sorted_memos:
            try:
                timestamp_utc = datetime.fromisoformat(memo['created_at'].replace('Z', '+00:00'))
                date_str = timestamp_utc.astimezone(JST).strftime('%Y-%m-%d')
                memos_by_date.setdefault(date_str, []).append(memo)
            except (KeyError, ValueError):
                pass
        
        all_success = True
        for date_str, memos_in_date in memos_by_date.items():
            file_path = f"{DROPBOX_VAULT_PATH}/DailyNotes/{date_str}.md"
            try:
                _, res = dbx.files_download(file_path)
                current_content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, dropbox.files.DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    current_content = ""
                    logging.info(f"[DROPBOX] {file_path} は存在しないため、新規作成します。")
                else:
                    logging.error(f"[DROPBOX] {file_path} のダウンロードに失敗しました: {e}")
                    all_success = False
                    continue
            
            lines_to_append = []
            for memo in memos_in_date:
                time_str = datetime.fromisoformat(memo['created_at'].replace('Z', '+00:00')).astimezone(JST).strftime('%H:%M')
                content_lines = memo['content'].strip().split('\n')
                formatted_content = f"- {content_lines[0]}"
                if len(content_lines) > 1:
                    formatted_content += "\n" + "\n".join([f"\t- {line}" for line in content_lines[1:]])
                lines_to_append.append(f"- {time_str}\n\t{formatted_content}")

            new_content = current_content
            if new_content and not new_content.endswith("\n"):
                new_content += "\n"
            new_content += "\n".join(lines_to_append)

            try:
                dbx.files_upload(new_content.encode('utf-8'), file_path, mode=WriteMode('overwrite'))
                logging.info(f"[DROPBOX] {file_path} の更新に成功しました。")
            except ApiError as e:
                logging.error(f"[DROPBOX] {file_path} のアップロードに失敗しました: {e}")
                all_success = False
        
        if all_success and sorted_memos:
            last_id = sorted_memos[-1]['id']
            try:
                LAST_PROCESSED_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(LAST_PROCESSED_ID_FILE, "w", encoding="utf-8") as f:
                    f.write(str(last_id))
                logging.info(f"[PROCESS] 最終処理IDを保存しました: {last_id}")
            except Exception as e:
                logging.error(f"[PROCESS] 最終処理IDの保存に失敗: {e}")
            
            with open(PENDING_MEMOS_FILE, "w") as f:
                json.dump([], f)
    
    return all_success

def main():
    if not process_pending_memos():
        sys.exit(1)
    logging.info("--- 同期ワーカー正常完了 ---")

if __name__ == "__main__":
    main()