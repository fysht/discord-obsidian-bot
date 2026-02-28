import os
import io
import asyncio
import logging
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from utils.obsidian_utils import update_section

from config import TOKEN_FILE, SCOPES

class GoogleDriveService:
    def __init__(self, folder_id):
        self.folder_id = folder_id
        self.creds = None
        self._load_credentials()

    def _load_credentials(self):
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
        if not self.creds: self._load_credentials()
        if self.creds:
            return build('drive', 'v3', credentials=self.creds)
        return None

    async def find_file(self, service, parent_id, name):
        if not parent_id: return None
        query = f"'{parent_id}' in parents and name = '{name}' and trashed = false"
        try:
            results = await asyncio.to_thread(lambda: service.files().list(q=query, fields="files(id)").execute())
            files = results.get('files', [])
            return files[0]['id'] if files else None
        except Exception as e:
            return None

    async def create_folder(self, service, parent_id, name):
        if not parent_id: return None
        file_metadata = {'name': name, 'parents': [parent_id], 'mimeType': 'application/vnd.google-apps.folder'}
        file = await asyncio.to_thread(lambda: service.files().create(body=file_metadata, fields='id').execute())
        return file.get('id')

    # mime_type を引数で受け取れるように拡張（HabitのJSON保存用）
    async def upload_text(self, service, parent_id, name, content, mime_type='text/markdown'):
        if not parent_id: return None
        file_metadata = {'name': name, 'parents': [parent_id], 'mimeType': mime_type}
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype=mime_type, resumable=True)
        file = await asyncio.to_thread(lambda: service.files().create(body=file_metadata, media_body=media, fields='id').execute())
        return file.get('id')
    
    async def update_text(self, service, file_id, content, mime_type='text/markdown'):
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype=mime_type, resumable=True)
        await asyncio.to_thread(lambda: service.files().update(fileId=file_id, media_body=media).execute())

    async def read_text_file(self, service, file_id):
        try:
            request = service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: _, done = await asyncio.to_thread(downloader.next_chunk)
            return fh.getvalue().decode('utf-8')
        except Exception as e:
            return ""

    async def search_markdown_files(self, keywords, limit=3):
        service = self.get_service()
        if not service: return "Drive接続エラー"
        query = f"fullText contains '{keywords}' and mimeType = 'text/markdown' and trashed = false"
        try:
            results = await asyncio.to_thread(lambda: service.files().list(q=query, pageSize=limit, fields="files(id, name)").execute())
            files = results.get('files', [])
            if not files: return f"「{keywords}」に関連するメモは見つかりませんでした。"
            
            search_results = []
            for file in files:
                content = await self.read_text_file(service, file['id'])
                snippet = content[:500].replace("\n", " ") + "..."
                search_results.append(f"【ファイル名: {file['name']}】\n{snippet}\n")
            return "\n".join(search_results)
        except Exception as e:
            return f"検索中にエラーが発生しました: {e}"

    async def download_file(self, service, file_id, local_path):
        """指定したIDのファイルをローカルにダウンロードする（PDF読み込み用）"""
        try:
            request = service.files().get_media(fileId=file_id)
            fh = io.FileIO(local_path, 'wb')
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = await asyncio.to_thread(downloader.next_chunk)
            return True
        except Exception as e:
            logging.error(f"DriveService: Download error: {e}")
            return False