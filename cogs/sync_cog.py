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

# --- .env 読み込み ---
load_dotenv()

# --- ロギング設定 ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout
)
sys.stdout.reconfigure(encoding='utf-8')

# --- 基本設定 ---
PENDING_MEMOS_FILE = Path(os.getenv("PENDING_MEMOS_FILE", "/var/data/pending_memos.json"))
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
DROPBOX_VAULT_PATH = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault") 
LAST_PROCESSED_ID_FILE_PATH = f"{DROPBOX_VAULT_PATH}/.bot/last_processed_id.txt"
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
        except (json.JSONDecodeError, FileNotFoundError, ValueError):
            memos = []
            
        if not memos:
            return True

        logging.info(f"[PROCESS] {len(memos)} 件のメモをDropboxに保存します...")
        
        try:
            with dropbox.Dropbox(
                oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
                app_key=DROPBOX_APP_KEY,
                app_secret=DROPBOX_APP_SECRET
            ) as dbx:
                dbx.check_user()
                logging.info("[DROPBOX] Dropboxへの接続に成功しました。")

                sorted_memos = sorted(memos, key=lambda m: int(m['id']))
                memos_by_date = {}
                for memo in sorted_memos:
                    try:
                        timestamp_utc = datetime.fromisoformat(memo['created_at'].replace('Z', ''))
                        date_str = timestamp_utc.astimezone(JST).strftime('%Y-%m-%d')
                        memos_by_date.setdefault(date_str, []).append(memo)
                    except (KeyError, ValueError):
                        pass
                
                all_success = True
                for date_str, memos_in_date in memos_by_date.items():
                    file_path = f"{DROPBOX_VAULT_PATH}/DailyNotes/{date_str}.md"
                    current_content = ""
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
                        time_str = datetime.fromisoformat(memo['created_at'].replace('Z', '')).astimezone(JST).strftime('%H:%M')
                        content_lines = memo['content'].strip().split('\n')
                        formatted_content = f"- {content_lines[0]}"
                        if len(content_lines) > 1:
                            formatted_content += "\n" + "\n".join([f"\t- {line}" for line in content_lines[1:]])
                        lines_to_append.append(f"- {time_str}\n\t{formatted_content}")

                    memo_header = "## Memo"
                    lines = current_content.split('\n')
                    
                    try:
                        # 既存の "## Memo" セクションを探す
                        header_index = lines.index(memo_header)
                        insert_index = header_index + 1
                        # セクションの末尾（次の見出し or ファイルの終わり）まで進む
                        while insert_index < len(lines) and not lines[insert_index].startswith('## '):
                            insert_index += 1
                        
                        # 新しいメモを挿入
                        for line in reversed(lines_to_append):
                            lines.insert(insert_index, line)

                    except ValueError:
                        # "## Memo" セクションがない場合は、ファイルの末尾に追加
                        if current_content.strip():
                            lines.append("") # 空行を追加
                        lines.append(memo_header)
                        lines.extend(lines_to_append)

                    new_content = "\n".join(lines)

                    dbx.files_upload(new_content.encode('utf-8'), file_path, mode=WriteMode('overwrite'))
                    logging.info(f"[DROPBOX] {file_path} の更新に成功しました。")
                
                if all_success and sorted_memos:
                    last_id = sorted_memos[-1]['id']
                    dbx.files_upload(str(last_id).encode('utf-8'), LAST_PROCESSED_ID_FILE_PATH, mode=WriteMode('overwrite'))
                    logging.info(f"[PROCESS] 最終処理IDをDropboxに保存しました: {last_id}")
                
                with open(PENDING_MEMOS_FILE, "w") as f:
                    json.dump([], f)
            
            return all_success

        except Exception as e:
            logging.error(f"[DROPBOX] Dropbox処理中に予期せぬエラーが発生しました: {e}", exc_info=True)
            return False

def main():
    if not process_pending_memos():
        sys.exit(1)
    logging.info("--- 同期ワーカー正常完了 ---")

if __name__ == "__main__":
    main()