import os
import sys
import logging
from datetime import datetime, timedelta
import zoneinfo
from dotenv import load_dotenv
# --- 新しいライブラリ ---
from google import genai
# ----------------------

# Google Drive API Imports
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from googleapiclient.errors import HttpError
import io

# --- .env 読み込み ---
load_dotenv()

# --- ロギング設定 ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout
)
sys.stdout.reconfigure(encoding='utf-8')

# --- 定数・設定 ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID") # VaultのルートID
TOKEN_FILE = 'token.json'
SCOPES = ['https://www.googleapis.com/auth/drive']

# タイムゾーン設定
try:
    JST = zoneinfo.ZoneInfo("Asia/Tokyo")
except Exception:
    JST = datetime.timezone(timedelta(hours=9))

# --- Drive API Helper Functions (sync_worker.pyと同様) ---
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
            logging.error("有効なトークンがありません。")
            return None

    try:
        service = build('drive', 'v3', credentials=creds)
        return service
    except Exception as e:
        logging.error(f"Driveサービスの構築失敗: {e}")
        return None

def find_file_in_folder(service, folder_id, file_name, mime_type=None):
    """フォルダ内のファイルを検索"""
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

def read_text_file(service, file_id):
    """ファイルの内容を読み込む"""
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
    """ファイルを更新（上書き）する"""
    try:
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown', resumable=True)
        service.files().update(fileId=file_id, media_body=media).execute()
        return True
    except HttpError as e:
        logging.error(f"ファイル更新エラー (ID: {file_id}): {e}")
        return False

# --- Main Logic ---
def generate_summary():
    """本日のデイリーノートを読み込み、Geminiで要約して追記する"""
    if not GEMINI_API_KEY:
        logging.error("GEMINI_API_KEY が設定されていません。")
        return

    if not GOOGLE_DRIVE_FOLDER_ID:
        logging.error("GOOGLE_DRIVE_FOLDER_ID が設定されていません。")
        return

    # 1. Drive接続
    service = get_drive_service()
    if not service:
        return

    # 2. ファイル特定 (DailyNotes/YYYY-MM-DD.md)
    today = datetime.now(JST).date()
    date_str = today.strftime('%Y-%m-%d')
    file_name = f"{date_str}.md"

    # DailyNotesフォルダを探す
    daily_notes_folder_id = find_file_in_folder(service, GOOGLE_DRIVE_FOLDER_ID, "DailyNotes", "application/vnd.google-apps.folder")
    if not daily_notes_folder_id:
        logging.error("DailyNotesフォルダが見つかりません。")
        return

    # ファイルを探す
    file_id = find_file_in_folder(service, daily_notes_folder_id, file_name)
    if not file_id:
        logging.info(f"本日のノート ({file_name}) が見つかりません。")
        print("NO_MEMO_TODAY") # Cog側への通知
        return

    # 3. 内容読み込み
    content = read_text_file(service, file_id)
    if not content.strip():
        logging.info("ノートが空です。")
        print("NO_MEMO_TODAY")
        return

    logging.info(f"ノート読み込み完了: {len(content)} 文字")

    # 4. Geminiで要約生成
    try:
        # --- Client初期化と実行 ---
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        prompt = f"""
以下のObsidianのデイリーノートの内容を整理してください。

【指示】
1. 文末はすべて「である調（〜である、〜だ）」で統一してください。
2. 箇条書きを使用し、可能な限り元の情報をすべて拾ってください。
3. 構造化して整理することは推奨しますが、要約や大幅な削除はしないでください。
4. 【重要】「Discordに投稿した」「AIに報告した・話した」といった行動自体は記録せず、ユーザー自身が手動でメモを書いたように「〇〇をした」「〇〇をしたい」「〇〇について考えた」という一人称視点の事実や思考として記述してください。
「今日の出来事」「学んだこと」「ネクストアクション」などのセクションに分けても構いません。
Markdown形式で出力してください。

---
{content}
---
"""
        response = client.models.generate_content(
            model='gemini-2.5-pro',
            contents=prompt
        )
        summary_text = response.text
        logging.info("要約生成完了")
        # ------------------------

    except Exception as e:
        logging.error(f"Gemini APIエラー: {e}")
        return

    # 5. 追記と更新
    header = "\n\n## 🌙 本日のふりかえり (AI Summary)\n"
    new_content = content + header + summary_text
    
    if update_text_file(service, file_id, new_content):
        logging.info("デイリーノートに要約を追記しました。")
        print(summary_text) # Cog側への出力
    else:
        logging.error("ファイルの更新に失敗しました。")

if __name__ == "__main__":
    generate_summary()