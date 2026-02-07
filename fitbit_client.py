import os
import asyncio
import logging
from datetime import date
from typing import Optional, Dict, Any
import aiohttp
import base64
import io

# Google Drive API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

# --- 定数定義 ---
SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

class FitbitClient:
    """
    Fitbit APIとの通信を管理し、アクセストークンの更新を自動的に行うクライアント
    (Google Drive版)
    """
    def __init__(self, client_id: str, client_secret: str, dbx=None, user_id: str = "-"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_id = user_id
        self.session = aiohttp.ClientSession()
        self.lock = asyncio.Lock()
        
        # Dropboxオブジェクト(dbx)は使用しませんが、互換性のため引数は残しています
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.bot_folder_name = ".bot"
        self.token_file_name = "fitbit_refresh_token.txt"

    def _get_drive_service(self):
        """Google Drive Serviceを取得"""
        creds = None
        if os.path.exists(TOKEN_FILE):
            try:
                creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            except Exception:
                pass

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    with open(TOKEN_FILE, 'w') as token:
                        token.write(creds.to_json())
                except Exception:
                    return None
            else:
                return None

        return build('drive', 'v3', credentials=creds)

    def _find_file(self, service, parent_id, name, mime_type=None):
        """ファイルIDを検索"""
        query = f"'{parent_id}' in parents and name = '{name}' and trashed = false"
        if mime_type:
            query += f" and mimeType = '{mime_type}'"
        
        try:
            results = service.files().list(q=query, fields="files(id)").execute()
            files = results.get('files', [])
            return files[0]['id'] if files else None
        except Exception as e:
            logging.error(f"Drive検索エラー: {e}")
            return None

    def _create_folder(self, service, parent_id, name):
        """フォルダを作成"""
        file_metadata = {
            'name': name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_id]
        }
        file = service.files().create(body=file_metadata, fields='id').execute()
        return file.get('id')

    def _read_file_content(self, service, file_id) -> str:
        """ファイルの内容をテキストとして読み込む"""
        try:
            request = service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()
            return fh.getvalue().decode('utf-8')
        except Exception as e:
            logging.error(f"Drive読み込みエラー: {e}")
            return ""

    def _write_file_content(self, service, parent_id, name, content, file_id=None):
        """ファイルの内容を書き込む（新規作成または更新）"""
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/plain', resumable=True)
        try:
            if file_id:
                service.files().update(fileId=file_id, media_body=media).execute()
            else:
                file_metadata = {'name': name, 'parents': [parent_id]}
                service.files().create(body=file_metadata, media_body=media).execute()
        except Exception as e:
            logging.error(f"Drive書き込みエラー: {e}")

    async def _get_latest_refresh_token(self) -> str:
        """Google Driveから最新のリフレッシュトークンを読み込む"""
        if not self.drive_folder_id:
            logging.warning("GOOGLE_DRIVE_FOLDER_IDが設定されていません。環境変数の初期トークンを使用します。")
            return os.getenv("FITBIT_REFRESH_TOKEN")

        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service:
            return os.getenv("FITBIT_REFRESH_TOKEN")

        # .botフォルダを探す
        bot_folder_id = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, self.bot_folder_name)
        if not bot_folder_id:
            return os.getenv("FITBIT_REFRESH_TOKEN")

        # トークンファイルを探す
        token_file_id = await loop.run_in_executor(None, self._find_file, service, bot_folder_id, self.token_file_name)
        if not token_file_id:
            return os.getenv("FITBIT_REFRESH_TOKEN")

        # 読み込み
        token = await loop.run_in_executor(None, self._read_file_content, service, token_file_id)
        if token:
            logging.info("Google Driveから最新のFitbitリフレッシュトークンを読み込みました。")
            return token.strip()
        
        return os.getenv("FITBIT_REFRESH_TOKEN")

    def _save_new_refresh_token(self, new_token: str):
        """新しいリフレッシュトークンをGoogle Driveに保存する"""
        if not self.drive_folder_id: return

        # 非同期コンテキスト外から呼ばれる可能性があるため、簡易的に同期実行するか、呼び出し元でawaitする必要がありますが、
        # ここではコード構造を維持するため、内部でイベントループを取得して実行を試みます。
        # ただし、aiohttpのコールバック内で呼ばれる場合を考慮し、例外処理を含めます。
        
        async def _async_save():
            loop = asyncio.get_running_loop()
            service = await loop.run_in_executor(None, self._get_drive_service)
            if not service: return

            bot_folder_id = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, self.bot_folder_name)
            if not bot_folder_id:
                bot_folder_id = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, self.bot_folder_name)

            token_file_id = await loop.run_in_executor(None, self._find_file, service, bot_folder_id, self.token_file_name)
            
            await loop.run_in_executor(None, self._write_file_content, service, bot_folder_id, self.token_file_name, new_token, token_file_id)
            logging.info(f"新しいFitbitリフレッシュトークンをGoogle Driveに保存しました: {self.token_file_name}")

        try:
            # 既にループが走っている場合（通常はこちら）
            loop = asyncio.get_running_loop()
            loop.create_task(_async_save())
        except RuntimeError:
            # ループがない場合（初期化時など）
            asyncio.run(_async_save())

    async def _get_access_token(self) -> Optional[str]:
        """リフレッシュトークンを使って新しいアクセストークンを取得する"""
        url = "https://api.fitbit.com/oauth2/token"
        auth_header = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        headers = {
            "Authorization": f"Basic {auth_header}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        current_refresh_token = await self._get_latest_refresh_token()
        
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": current_refresh_token
        }
        
        async with self.session.post(url, headers=headers, data=payload) as resp:
            response_json = await resp.json()
            
            if resp.status == 200:
                logging.info("アクセストークンの取得に成功しました。")
                access_token = response_json.get("access_token")
                new_refresh_token = response_json.get("refresh_token")
                self._save_new_refresh_token(new_refresh_token)
                return access_token
            else:
                logging.error(f"トークン交換APIからのエラー (ステータス: {resp.status}): {response_json}")
                return None

    async def get_sleep_data(self, target_date: date) -> Optional[Dict[str, Any]]:
        access_token = await self._get_access_token()
        if not access_token: return None
        date_str = target_date.strftime('%Y-%m-%d')
        url = f"https://api.fitbit.com/1.2/user/{self.user_id}/sleep/date/{date_str}.json"
        headers = {"Authorization": f"Bearer {access_token}"}
        
        async with self.session.get(url, headers=headers) as resp:
            response_json = await resp.json()
            if resp.status == 200 and response_json.get('sleep'):
                logging.info(f"{target_date} の睡眠データを正常に取得しました。")
                return response_json
            else:
                logging.error(f"睡眠データAPIからのエラー (ステータス: {resp.status}): {response_json}")
                return None

    async def get_activity_summary(self, target_date: date) -> Optional[Dict[str, Any]]:
        access_token = await self._get_access_token()
        if not access_token: return None
        date_str = target_date.strftime('%Y-%m-%d')
        url = f"https://api.fitbit.com/1/user/{self.user_id}/activities/date/{date_str}.json"
        headers = {"Authorization": f"Bearer {access_token}"}

        async with self.session.get(url, headers=headers) as resp:
            response_json = await resp.json()
            if resp.status == 200 and 'summary' in response_json:
                logging.info(f"{target_date} の活動概要データを正常に取得しました。")
                heart_rate_zones = response_json.get('summary', {}).get('heartRateZones', [])
                response_json['summary']['heartRateZones'] = {zone['name']: zone for zone in heart_rate_zones}
                return response_json
            else:
                logging.error(f"活動概要データAPIからのエラー (ステータス: {resp.status}): {response_json}")
                return None