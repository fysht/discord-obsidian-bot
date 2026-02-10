import os
import io
import asyncio
import logging
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from utils.obsidian_utils import update_section

SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

class DriveService:
    def __init__(self, folder_id):
        self.folder_id = folder_id
        self.creds = None
        self._load_credentials()

    def _load_credentials(self):
        """トークンの読み込みと更新"""
        if os.path.exists(TOKEN_FILE):
            try:
                self.creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            except Exception as e:
                logging.error(f"DriveService: Token read error: {e}")

        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                try:
                    self.creds.refresh(Request())
                    with open(TOKEN_FILE, 'w') as token:
                        token.write(self.creds.to_json())
                except Exception as e:
                    logging.error(f"DriveService: Token refresh error: {e}")
                    self.creds = None
            else:
                logging.warning("DriveService: Valid token not found.")
                self.creds = None

    def get_service(self):
        """Drive APIサービスビルダを取得"""
        if not self.creds:
            self._load_credentials()
        
        if self.creds:
            try:
                return build('drive', 'v3', credentials=self.creds)
            except Exception as e:
                logging.error(f"DriveService: Init Error: {e}")
        return None

    async def find_file(self, service, parent_id, name):
        """ファイル/フォルダID検索"""
        if not parent_id: return None
        query = f"'{parent_id}' in parents and name = '{name}' and trashed = false"
        try:
            results = await asyncio.to_thread(
                lambda: service.files().list(q=query, fields="files(id)").execute()
            )
            files = results.get('files', [])
            return files[0]['id'] if files else None
        except Exception as e:
            logging.error(f"Find File Error: {e}")
            return None

    async def create_folder(self, service, parent_id, name):
        """フォルダ作成"""
        if not parent_id: return None
        file_metadata = {
            'name': name,
            'parents': [parent_id],
            'mimeType': 'application/vnd.google-apps.folder'
        }
        file = await asyncio.to_thread(
            lambda: service.files().create(body=file_metadata, fields='id').execute()
        )
        return file.get('id')

    async def upload_text(self, service, parent_id, name, content):
        """テキストアップロード"""
        if not parent_id: return None
        file_metadata = {'name': name, 'parents': [parent_id], 'mimeType': 'text/markdown'}
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown', resumable=True)
        file = await asyncio.to_thread(
            lambda: service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        )
        return file.get('id')

    async def read_text_file(self, service, file_id):
        """テキスト読み込み"""
        try:
            request = service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: _, done = await asyncio.to_thread(downloader.next_chunk)
            return fh.getvalue().decode('utf-8')
        except Exception as e:
            logging.error(f"Read File Error: {e}")
            return ""

    async def update_daily_note(self, service, date_str, link_text, section_header):
        """デイリーノートへの追記"""
        if not self.folder_id: return

        loop = asyncio.get_running_loop()
        
        # フォルダ確認・作成
        daily_folder = await self.find_file(service, self.folder_id, "DailyNotes")
        if not daily_folder:
            daily_folder = await self.create_folder(service, self.folder_id, "DailyNotes")

        # ファイル確認・作成
        filename = f"{date_str}.md"
        f_id = await self.find_file(service, daily_folder, filename)
        
        content = ""
        if f_id:
            content = await self.read_text_file(service, f_id)
        else:
            content = f"# Daily Note {date_str}\n\n"

        # utilsの関数を使って追記
        new_content = update_section(content, link_text, section_header)

        media = MediaIoBaseUpload(io.BytesIO(new_content.encode('utf-8')), mimetype='text/markdown', resumable=True)
        
        if f_id:
            await loop.run_in_executor(None, lambda: service.files().update(fileId=f_id, media_body=media).execute())
        else:
            await loop.run_in_executor(None, lambda: service.files().create(body={'name': filename, 'parents': [daily_folder]}, media_body=media).execute())