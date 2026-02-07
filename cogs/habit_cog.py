import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import json
import asyncio
import datetime
import zoneinfo
# Google API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
import io

try: from utils.obsidian_utils import update_frontmatter
except ImportError: def update_frontmatter(content, updates): return content

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
HABIT_DATA_FILE = "habit_data.json"
BOT_FOLDER = ".bot"
SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

class HabitCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.channel_id = int(os.getenv("NEWS_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.daily_task.start()

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

    def _find_file(self, service, parent_id, name):
        res = service.files().list(q=f"'{parent_id}' in parents and name = '{name}' and trashed = false", fields="files(id)").execute()
        files = res.get('files', [])
        return files[0]['id'] if files else None

    async def _load_data(self):
        default = {"habits": [], "logs": {}}
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return default
        
        b_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, BOT_FOLDER)
        if not b_folder: return default
        
        f_id = await loop.run_in_executor(None, self._find_file, service, b_folder, HABIT_DATA_FILE)
        if f_id:
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=f_id))
            done=False
            while not done: _, done = downloader.next_chunk()
            return json.loads(fh.getvalue().decode('utf-8'))
        return default

    async def _save_data(self, data):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return False
        
        b_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, BOT_FOLDER)
        # Create folder logic omitted
        
        f_id = await loop.run_in_executor(None, self._find_file, service, b_folder, HABIT_DATA_FILE)
        media = MediaIoBaseUpload(io.BytesIO(json.dumps(data, ensure_ascii=False).encode('utf-8')), mimetype='application/json')
        
        if f_id: await loop.run_in_executor(None, lambda: service.files().update(fileId=f_id, media_body=media).execute())
        else: await loop.run_in_executor(None, lambda: service.files().create(body={'name': HABIT_DATA_FILE, 'parents': [b_folder]}, media_body=media).execute())
        return True

    # ... (Tasks and commands logic remain similar, calling _load_data/_save_data) ...
    # _sync_to_obsidian_daily needs update
    async def _sync_to_obsidian_daily(self, data, date_str):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        
        daily_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, "DailyNotes")
        f_id = await loop.run_in_executor(None, self._find_file, service, daily_folder, f"{date_str}.md")
        
        content = f"# Daily Note {date_str}\n"
        if f_id:
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=f_id))
            done=False
            while not done: _, done = downloader.next_chunk()
            content = fh.getvalue().decode('utf-8')

        daily_log = data['logs'].get(date_str, [])
        completed = [h['name'] for h in data['habits'] if h['id'] in daily_log]
        
        new_content = update_frontmatter(content, {"habits": completed})
        
        media = MediaIoBaseUpload(io.BytesIO(new_content.encode('utf-8')), mimetype='text/markdown')
        if f_id: await loop.run_in_executor(None, lambda: service.files().update(fileId=f_id, media_body=media).execute())
        else: await loop.run_in_executor(None, lambda: service.files().create(body={'name': f"{date_str}.md", 'parents': [daily_folder]}, media_body=media).execute())

async def setup(bot): await bot.add_cog(HabitCog(bot))