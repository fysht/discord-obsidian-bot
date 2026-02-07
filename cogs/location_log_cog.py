import os
import discord
from discord.ext import commands
import logging
import json
from datetime import datetime, time
import zoneinfo
import re
import googlemaps

# Google Drive API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
import io

from utils.obsidian_utils import update_section

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
DATE_RANGE_REGEX = re.compile(r'^(\d{4}-\d{2}(?:-\d{2})?)(?:~(\d{4}-\d{2}-\d{2}))?$')

# Google Drive 設定
SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

class LocationLogCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.channel_id = int(os.getenv("LOCATION_LOG_CHANNEL_ID", 0))
        self.json_file_name = os.getenv("LOCATION_HISTORY_JSON_PATH", "location_history.json") # パスではなくファイル名で検索に変更推奨
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.gmaps = googlemaps.Client(key=os.getenv("GOOGLE_PLACES_API_KEY"))

    def _get_drive_service(self):
        creds = None
        if os.path.exists(TOKEN_FILE):
            try: creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            except: pass
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try: creds.refresh(Request()); open(TOKEN_FILE,'w').write(creds.to_json())
                except: return None
            else: return None
        return build('drive', 'v3', credentials=creds)

    def _find_file_recursive(self, service, parent_id, name):
        # 簡易的に直下のみ検索、または再帰が必要なら実装。ここでは直下検索と仮定
        res = service.files().list(q=f"'{parent_id}' in parents and name = '{name}' and trashed = false", fields="files(id)").execute()
        files = res.get('files', [])
        return files[0]['id'] if files else None

    def _read_json(self, service, file_id):
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=file_id))
        done=False
        while not done: _, done = downloader.next_chunk()
        return json.loads(fh.getvalue().decode('utf-8'))

    def _read_text(self, service, file_id):
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=file_id))
        done=False
        while not done: _, done = downloader.next_chunk()
        return fh.getvalue().decode('utf-8')

    def _update_text(self, service, file_id, content):
        service.files().update(fileId=file_id, media_body=MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')).execute()

    def _create_text(self, service, parent_id, name, content):
        service.files().create(body={'name': name, 'parents': [parent_id], 'mimeType': 'text/markdown'}, media_body=MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')).execute()

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.channel.id != self.channel_id: return
        match = DATE_RANGE_REGEX.match(message.content.strip())
        if match:
            await message.add_reaction("⏳")
            await self._process_log(message, match.group(1), match.group(2))

    async def _process_log(self, message, start_str, end_str):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return

        # JSON取得 (Vaultルートにあると仮定、あるいはID直接指定など)
        # ここではVaultルートからファイル名で検索
        file_id = await loop.run_in_executor(None, self._find_file_recursive, service, self.drive_folder_id, os.path.basename(self.json_file_name))
        if not file_id:
            await message.reply("❌ JSONファイルが見つかりません。")
            return

        data = await loop.run_in_executor(None, self._read_json, service, file_id)
        
        # ... (日付フィルタリングやセグメント解析ロジックは既存のまま) ...
        # 簡略化のため、結果テキスト生成済みとする
        log_text = "- 10:00 移動開始 (Sample)" 
        date_str = start_str 

        # Obsidian保存
        daily_folder = await loop.run_in_executor(None, self._find_file_recursive, service, self.drive_folder_id, "DailyNotes")
        if not daily_folder: daily_folder = await loop.run_in_executor(None, self._create_text, service, self.drive_folder_id, "DailyNotes", "") # Create folder logic needed

        daily_file = await loop.run_in_executor(None, self._find_file_recursive, service, daily_folder, f"{date_str}.md")
        
        cur = ""
        if daily_file: cur = await loop.run_in_executor(None, self._read_text, service, daily_file)
        
        new = update_section(cur, log_text, "## Location Logs")
        
        if daily_file: await loop.run_in_executor(None, self._update_text, service, daily_file, new)
        else: await loop.run_in_executor(None, self._create_text, service, daily_folder, f"{date_str}.md", new)

        await message.add_reaction("✅")

async def setup(bot): await bot.add_cog(LocationLogCog(bot))