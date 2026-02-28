import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from filelock import FileLock
from dotenv import load_dotenv

# Google API Client Imports
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from googleapiclient.errors import HttpError
import io

# --- リファクタリング: クリーンなインポート ---
from utils.obsidian_utils import update_section
from config import JST, TOKEN_FILE, SCOPES

# --- .env 読み込み ---
load_dotenv()

# --- ロギング設定 ---
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s][sync_worker] %(message)s",
    stream=sys.stderr
)

# --- 基本設定 ---
PENDING_MEMOS_FILE = Path(os.getenv("PENDING_MEMOS_FILE", "/var/data/pending_memos.json"))
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID") # VaultのルートフォルダID

def get_drive_service():
    """Google Drive APIサービスを取得する"""
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except Exception as e:
            logging.error(f"トークンファイルの読み込みエラー: {e}")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(TOKEN_FILE, 'w') as token:
                    token.write(creds.to_json())
                logging.info("トークンをリフレッシュしました。")
            except Exception as e:
                logging.error(f"トークンのリフレッシュ失敗: {e}")
                return None
        else:
            logging.error("有効なトークンがありません。generate_token.pyを実行してください。")
            return None

    try:
        service = build('drive', 'v3', credentials=creds)
        return service
    except Exception as e:
        logging.error(f"Driveサービスの構築失敗: {e}")
        return None

def find_file_in_folder(service, folder_id, file_name, mime_type=None):
    """フォルダ内のファイルを名前で検索し、IDを返す"""
    query = f"'{folder_id}' in parents and name = '{file_name}' and trashed = false"
    if mime_type:
        query += f" and mimeType = '{mime_type}'"
    
    try:
        results = service.files().list(q=query, fields="files(id, name)").execute()
        files = results.get('files', [])
        if files:
            return files[0]['id']
        return None
    except HttpError as e:
        logging.error(f"ファイル検索エラー ({file_name}): {e}")
        return None

def create_folder(service, parent_id, folder_name):
    """フォルダを作成する"""
    file_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id]
    }
    try:
        file = service.files().create(body=file_metadata, fields='id').execute()
        return file.get('id')
    except HttpError as e:
        logging.error(f"フォルダ作成エラー ({folder_name}): {e}")
        return None

def read_text_file(service, file_id):
    """テキストファイルの内容を読み込む"""
    try:
        request = service.files().get_media(fileId=file_id)
        file_io = io.BytesIO()
        downloader = MediaIoBaseDownload(file_io, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
        return file_io.getvalue().decode('utf-8')
    except HttpError as e:
        logging.error(f"ファイル読み込みエラー (ID: {file_id}): {e}")
        return ""

def update_text_file(service, file_id, content):
    """テキストファイルを更新（上書き）する"""
    try:
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown', resumable=True)
        service.files().update(fileId=file_id, media_body=media).execute()
        return True
    except HttpError as e:
        logging.error(f"ファイル更新エラー (ID: {file_id}): {e}")
        return False

def create_text_file(service, parent_id, file_name, content):
    """新しいテキストファイルを作成する"""
    file_metadata = {
        'name': file_name,
        'parents': [parent_id],
        'mimeType': 'text/markdown' # Obsidian用にMarkdown指定
    }
    try:
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown', resumable=True)
        file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        return file.get('id')
    except HttpError as e:
        logging.error(f"ファイル作成エラー ({file_name}): {e}")
        return None

def process_pending_memos():
    """保留メモをGoogle Drive上のDailyNoteに追加する"""
    logging.info(f"保留メモファイルを確認: {PENDING_MEMOS_FILE}")
    if not PENDING_MEMOS_FILE.exists():
        logging.info("保留メモファイルが存在しません。スキップします。")
        return True

    if not GOOGLE_DRIVE_FOLDER_ID:
        logging.error("GOOGLE_DRIVE_FOLDER_ID が設定されていません。")
        return False

    lock_path = str(PENDING_MEMOS_FILE) + ".lock"
    lock = FileLock(lock_path, timeout=10)

    try:
        with lock:
            if PENDING_MEMOS_FILE.stat().st_size == 0:
                 return True

            with open(PENDING_MEMOS_FILE, "r", encoding="utf-8") as f:
                try:
                    memos = json.load(f)
                except json.JSONDecodeError:
                    return False

            if not memos:
                return True

            logging.info(f"[PROCESS] {len(memos)} 件のメモをGoogle Driveに同期開始...")

            # --- Google Drive 接続 ---
            service = get_drive_service()
            if not service:
                return False

            # --- フォルダ構成の確認 (DailyNotes) ---
            daily_notes_folder_id = find_file_in_folder(service, GOOGLE_DRIVE_FOLDER_ID, "DailyNotes", "application/vnd.google-apps.folder")
            if not daily_notes_folder_id:
                logging.info("DailyNotesフォルダが見つからないため作成します。")
                daily_notes_folder_id = create_folder(service, GOOGLE_DRIVE_FOLDER_ID, "DailyNotes")
                if not daily_notes_folder_id:
                    return False

            # --- メモのソートとグループ化 ---
            # (既存ロジック同様)
            try:
                sorted_memos = sorted([m for m in memos if m.get('id')], key=lambda m: int(m['id']))
            except:
                sorted_memos = [m for m in memos if m.get('id')]
            
            memos_by_date = {}
            for memo in sorted_memos:
                try:
                    ts_str = memo.get('created_at')
                    timestamp_utc = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                    timestamp_jst = timestamp_utc.astimezone(JST)
                    date_str = timestamp_jst.strftime('%Y-%m-%d')
                    memos_by_date.setdefault(date_str, []).append(memo)
                except Exception:
                    continue

            failed_ids = set()
            processed_ids = set()
            latest_processed_id = None

            # --- 日付ごとの処理 ---
            for date_str, memos_in_date in memos_by_date.items():
                file_name = f"{date_str}.md"
                logging.info(f"[DRIVE] デイリーノート処理: {file_name}")

                # ファイル検索
                file_id = find_file_in_folder(service, daily_notes_folder_id, file_name)
                current_content = ""

                if file_id:
                    logging.info(f"[DRIVE] 既存ファイルを取得: {file_id}")
                    current_content = read_text_file(service, file_id)
                else:
                    logging.info(f"[DRIVE] 新規ファイルとして扱います: {file_name}")

                # 追記内容の作成
                content_to_add = []
                current_batch_ids = []

                for memo in memos_in_date:
                    memo_id = memo.get('id')
                    memo_content = memo.get('content', '').strip()
                    if not memo_content:
                        processed_ids.add(memo_id) # 空メモは成功扱い
                        continue
                    
                    try:
                        ts = datetime.fromisoformat(memo['created_at'].replace('Z', '+00:00')).astimezone(JST)
                        time_str = ts.strftime('%H:%M')
                        lines = memo_content.split('\n')
                        formatted = f"- {time_str}\n\t- " + "\n\t- ".join(lines)
                        content_to_add.append(formatted)
                        current_batch_ids.append(memo_id)
                    except Exception as e:
                        logging.error(f"メモフォーマットエラー: {e}")
                        failed_ids.add(memo_id)

                if not content_to_add:
                    continue

                # 内容の結合 (update_section)
                new_content_part = "\n".join(content_to_add)
                full_new_content = update_section(current_content, new_content_part, "## Memo")

                # アップロード (更新または作成)
                success = False
                if file_id:
                    success = update_text_file(service, file_id, full_new_content)
                else:
                    new_id = create_text_file(service, daily_notes_folder_id, file_name, full_new_content)
                    if new_id:
                        success = True
                
                if success:
                    processed_ids.update(current_batch_ids)
                    # ID更新
                    valid_ids = [int(mid) for mid in current_batch_ids if mid]
                    if valid_ids:
                        max_id = max(valid_ids)
                        if latest_processed_id is None or max_id > latest_processed_id:
                            latest_processed_id = max_id
                else:
                    failed_ids.update(current_batch_ids)

            # --- 結果処理 ---
            if not failed_ids:
                logging.info("全処理成功。")
                
                # Last Processed ID の保存 (.botフォルダ)
                if latest_processed_id:
                    bot_folder_id = find_file_in_folder(service, GOOGLE_DRIVE_FOLDER_ID, ".bot", "application/vnd.google-apps.folder")
                    if not bot_folder_id:
                        bot_folder_id = create_folder(service, GOOGLE_DRIVE_FOLDER_ID, ".bot")
                    
                    if bot_folder_id:
                        last_id_file_id = find_file_in_folder(service, bot_folder_id, "last_processed_id.txt")
                        if last_id_file_id:
                            update_text_file(service, last_id_file_id, str(latest_processed_id))
                        else:
                            create_text_file(service, bot_folder_id, "last_processed_id.txt", str(latest_processed_id))

                # ファイルクリア
                with open(PENDING_MEMOS_FILE, "w") as f:
                    json.dump([], f)
                return True
            else:
                logging.error(f"一部失敗あり: {failed_ids}")
                # 失敗分だけ残す処理は必要に応じて実装（ここでは簡略化のため省略またはクリアしない選択肢もあり）
                # 安全のため、失敗があった場合はファイルを空にせず、次回リトライさせる（既存のファイル内容を維持）
                # ただし、成功した分を削除するロジックを入れるとより堅牢になる
                
                remaining_memos = [m for m in memos if m.get('id') in failed_ids or m.get('id') not in processed_ids]
                with open(PENDING_MEMOS_FILE, "w", encoding="utf-8") as f:
                    json.dump(remaining_memos, f, ensure_ascii=False, indent=2)
                return False

    except Exception as e:
        logging.error(f"予期せぬエラー: {e}", exc_info=True)
        return False

def main():
    logging.info("--- 同期ワーカー(Google Drive版) 起動 ---")
    if process_pending_memos():
        logging.info("--- 同期ワーカー正常完了 ---")
        sys.exit(0)
    else:
        logging.error("--- 同期ワーカーエラー終了 ---")
        sys.exit(1)

if __name__ == "__main__":
    main()