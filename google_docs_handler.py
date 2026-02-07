import os
import logging
import asyncio
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from dotenv import load_dotenv
from datetime import datetime, timedelta
import zoneinfo

# .env読み込み
load_dotenv()

# --- 定数定義 ---
try:
    JST = zoneinfo.ZoneInfo("Asia/Tokyo")
except Exception:
    JST = datetime.timezone(timedelta(hours=9))

# --- 設定 ---
TOKEN_FILE = 'token.json'
# ★修正: Drive APIのスコープを追加して統一
SCOPES = [
    'https://www.googleapis.com/auth/documents',
    'https://www.googleapis.com/auth/drive'
]
TARGET_DOCUMENT_ID = os.getenv("NOTEBOOKLM_GOOGLE_DOC_ID")

# --- ロギング設定 ---
logger = logging.getLogger(__name__)

google_creds = None
service = None

def _get_google_creds():
    """Google API認証情報の取得と更新"""
    global google_creds, service
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            logger.debug("Google Docs API: token.jsonを読み込みました。")
        except Exception as e:
            logger.warning(f"Google Docs API: 既存の{TOKEN_FILE}の読み込みに失敗: {e}")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Google Docs API: トークンが期限切れです。リフレッシュを試みます...")
            try:
                creds.refresh(Request())
                logger.info("Google Docs API: トークンのリフレッシュに成功しました。")
                with open(TOKEN_FILE, 'w') as token:
                    token.write(creds.to_json())
                logger.info(f"Google Docs API: 更新された{TOKEN_FILE}を保存しました。")
            except Exception as e:
                logger.error(f"Google Docs API: トークンのリフレッシュに失敗: {e}")
                creds = None
        else:
            # ここでは自動生成せず、環境変数からの復元やエラーログのみとする（main.pyで復元される前提）
            logger.error(f"Google Docs API: 有効な認証情報が見つかりません。token.jsonを確認してください。")
            creds = None

    google_creds = creds
    if google_creds:
        try:
            service = build('docs', 'v1', credentials=google_creds)
            logger.info("Google Docs APIサービスを初期化しました。")
        except Exception as e:
            logger.error(f"Google Docs APIサービスのビルドに失敗: {e}")
            service = None
    else:
        service = None

def _append_text_to_doc_sync(text_to_append: str, source_type: str = "Memo", url: str = None, title: str = None):
    """Googleドキュメントの末尾にフォーマットされたテキストを追記する（同期処理）"""
    if not service:
        logger.error("Google Docs APIサービスが利用できません。追記をスキップします。")
        _get_google_creds() # 再初期化を試みる
        if not service: return

    if not TARGET_DOCUMENT_ID:
        logger.error("環境変数 NOTEBOOKLM_GOOGLE_DOC_ID が設定されていません。")
        return

    try:
        # ドキュメントの末尾のインデックスを取得
        document = service.documents().get(documentId=TARGET_DOCUMENT_ID, fields='body(content(endIndex))').execute()
        content = document.get('body', {}).get('content', [])
        
        end_index = 1
        if content:
             last_segment_end_index = content[-1].get('endIndex')
             if last_segment_end_index:
                  end_index = max(1, last_segment_end_index -1)

        # --- NotebookLM向けのフォーマット作成 ---
        now_jst = datetime.now(JST)
        date_str = now_jst.strftime('%Y-%m-%d %H:%M:%S JST')
        header = f"\n--- Date: {date_str} (Source: {source_type}) ---"
        footer = "--- End ---"
        content_lines = [header]
        if title:
            content_lines.append(f"Title: {title}")
        if url:
            content_lines.append(f"URL: {url}")
        
        main_content = text_to_append.strip()
        if main_content:
             content_lines.append("\n" + main_content)
        content_lines.append("\n" + footer)

        formatted_text = "\n".join(content_lines) + "\n"

        requests = [
            {
                'insertText': {
                    'location': {
                        'index': end_index,
                    },
                    'text': formatted_text
                }
            }
        ]
        service.documents().batchUpdate(
            documentId=TARGET_DOCUMENT_ID, body={'requests': requests}
        ).execute()
        logger.info(f"Googleドキュメント (ID: ...{TARGET_DOCUMENT_ID[-6:]}) に追記しました ({source_type})。")

    except HttpError as e:
        logger.error(f"Googleドキュメントへの追記中にHttpErrorが発生: Status {e.resp.status}")
        if e.resp.status == 401 or e.resp.status == 403:
            logger.error("認証エラーが発生しました。token.jsonのスコープを確認してください。")
            _get_google_creds()
    except Exception as e:
        logger.error(f"Googleドキュメントへの追記中に予期せぬエラーが発生しました: {e}", exc_info=True)

async def append_text_to_doc_async(text_to_append: str, source_type: str = "Memo", url: str = None, title: str = None):
    """Googleドキュメント追記の同期処理を非同期で呼び出す"""
    await asyncio.to_thread(_append_text_to_doc_sync, text_to_append, source_type, url, title)

# モジュールロード時に認証を試みる
_get_google_creds()