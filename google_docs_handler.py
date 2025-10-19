import os
import logging
import asyncio
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from dotenv import load_dotenv
from datetime import datetime
import zoneinfo

# .env読み込み
load_dotenv()

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo") # JSTタイムゾーン

# --- 設定 ---
SCOPES = ['https://www.googleapis.com/auth/documents'] # Docs APIのスコープのみ
TOKEN_FILE = 'token.json'
CREDENTIALS_FILE = 'credentials.json' # generate_token.pyで使用
TARGET_DOCUMENT_ID = os.getenv("NOTEBOOKLM_GOOGLE_DOC_ID")

# --- ロギング設定 ---
# logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s') # main.pyで設定するため不要な場合が多い
logger = logging.getLogger(__name__) # 個別のロガーを取得

google_creds = None
service = None

def _get_google_creds():
    """Google API認証情報の取得と更新"""
    global google_creds, service
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            # SCOPESを指定して読み込む
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
            logger.error(f"Google Docs API: 有効な認証情報が見つかりません。{TOKEN_FILE}が存在しないか無効、またはスコープが不足している可能性があります。")
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
        if not service: return # 再初期化してもダメなら諦める

    if not TARGET_DOCUMENT_ID:
        logger.error("環境変数 NOTEBOOKLM_GOOGLE_DOC_ID が設定されていません。")
        return

    try:
        # ドキュメントの末尾のインデックスを取得（より確実に末尾に挿入するため）
        # fieldsを指定して取得する情報を限定する（パフォーマンス改善）
        document = service.documents().get(documentId=TARGET_DOCUMENT_ID, fields='body(content(endIndex))').execute()
        # ドキュメントが空の場合も考慮
        content = document.get('body', {}).get('content', [])
        end_index = 1 # デフォルトは先頭
        if content:
             # 最後の要素（通常は段落）の endIndex を使う
             # endIndexは次の挿入ポイントを示すので、-1 は不要
             end_index = content[-1].get('endIndex', 1)

        # --- NotebookLM向けのフォーマット作成 ---
        now_jst = datetime.now(JST)
        date_str = now_jst.strftime('%Y-%m-%d %H:%M:%S JST')
        header = f"\n--- Date: {date_str} (Source: {source_type}) ---" # 先頭に改行を追加
        footer = "--- End ---"
        content_lines = [header]
        if title:
            content_lines.append(f"Title: {title}")
        if url:
            content_lines.append(f"URL: {url}")
        # 本文が空でない場合のみ追記
        main_content = text_to_append.strip()
        if main_content:
             content_lines.append("\n" + main_content) # 本文の前に改行
        content_lines.append("\n" + footer) # フッターの前に改行

        formatted_text = "\n".join(content_lines) + "\n" # 全体の後に改行を追加
        # --- ここまで ---


        requests = [
            {
                'insertText': {
                    # ドキュメント末尾を示す endIndex を使う
                    # 空ドキュメントの場合は 1 から挿入される
                    'location': {
                        'index': end_index,
                    },
                    'text': formatted_text # フォーマット済みテキストを使用
                }
            }
        ]
        service.documents().batchUpdate(
            documentId=TARGET_DOCUMENT_ID, body={'requests': requests}
        ).execute()
        logger.info(f"Googleドキュメント (ID: ...{TARGET_DOCUMENT_ID[-6:]}) に追記しました ({source_type})。")

    except HttpError as e:
        logger.error(f"Googleドキュメントへの追記中にHttpErrorが発生: Status {e.resp.status}, Reason: {e.reason}, Content: {e.content}")
        if e.resp.status == 401 or e.resp.status == 403:
            logger.error("認証エラーが発生しました。token.jsonのスコープや有効期限を確認し、必要であれば再生成してください。")
            _get_google_creds() # 認証情報をリフレッシュ試行
    except Exception as e:
        logger.error(f"Googleドキュメントへの追記中に予期せぬエラーが発生しました: {e}", exc_info=True)

async def append_text_to_doc_async(text_to_append: str, source_type: str = "Memo", url: str = None, title: str = None):
    """Googleドキュメント追記の同期処理を非同期で呼び出す (引数追加)"""
    await asyncio.to_thread(_append_text_to_doc_sync, text_to_append, source_type, url, title)

# モジュールロード時に認証を試みる
_get_google_creds()