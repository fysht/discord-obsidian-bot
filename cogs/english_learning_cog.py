import os
import json
import asyncio
import logging
import discord
from discord.ext import commands, tasks
from discord import app_commands
import google.generativeai as genai
import re
from datetime import time, datetime
import zoneinfo
import aiohttp
import random

# Google API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
import io

try:
    from utils.obsidian_utils import update_section
except ImportError:
    def update_section(content, text, header): return f"{content}\n{header}\n{text}"

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
MORNING_SAKUBUN_TIME = time(hour=8, minute=0, tzinfo=JST)
EVENING_SAKUBUN_TIME = time(hour=21, minute=0, tzinfo=JST)
SAKUBUN_FILE_NAME = "瞬間英作文リスト.md"
ENGLISH_LOG_FOLDER = "English Learning"
SAKUBUN_LOG_FOLDER = "Sakubun Log"
BOT_FOLDER = ".bot"

SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

class EnglishLearningCog(commands.Cog, name="EnglishLearning"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = int(os.getenv("ENGLISH_LEARNING_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        genai.configure(api_key=self.gemini_api_key)
        self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
        self.session = aiohttp.ClientSession()
        self.chat_sessions = {}
        self.sakubun_questions = []
        self.is_ready = bool(self.drive_folder_id)

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
        # 簡易検索（直下のみ）
        res = service.files().list(q=f"'{parent_id}' in parents and name = '{name}' and trashed = false", fields="files(id)").execute()
        files = res.get('files', [])
        return files[0]['id'] if files else None

    # 再帰検索用（フォルダ構造が深い場合）
    def _find_path_id(self, service, path):
        parts = path.strip('/').split('/')
        current_id = self.drive_folder_id
        for part in parts:
            current_id = self._find_file_recursive(service, current_id, part)
            if not current_id: return None
        return current_id

    def _read_text(self, service, file_id):
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=file_id))
        done=False
        while not done: _, done = downloader.next_chunk()
        return fh.getvalue().decode('utf-8')

    def _create_text(self, service, parent_id, name, content):
        service.files().create(body={'name': name, 'parents': [parent_id], 'mimeType': 'text/markdown'}, media_body=MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')).execute()

    def _update_text(self, service, file_id, content):
        service.files().update(fileId=file_id, media_body=MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')).execute()

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready:
            await self._load_sakubun_questions()
            self.morning_sakubun_task.start()
            self.evening_sakubun_task.start()

    async def _load_sakubun_questions(self):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return
        
        # Studyフォルダ内のファイルを検索と仮定
        study_folder = await loop.run_in_executor(None, self._find_file_recursive, service, self.drive_folder_id, "Study")
        if not study_folder: return
        
        file_id = await loop.run_in_executor(None, self._find_file_recursive, service, study_folder, SAKUBUN_FILE_NAME)
        if file_id:
            content = await loop.run_in_executor(None, self._read_text, service, file_id)
            questions = re.findall(r'^\s*-\s*(.+?)(?:\s*::\s*.*)?$', content, re.MULTILINE)
            if questions: self.sakubun_questions = [q.strip() for q in questions if q.strip()]

    # ... (Tasks, Commands like morning_sakubun_task, english command are same logic) ...
    # _load_session_from_dropbox, _save_session_to_dropbox, _save_chat_log_to_obsidian, _save_sakubun_log_to_obsidian needs update

    async def _save_chat_log_to_obsidian(self, user, history, review):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return

        now = datetime.now(JST); date_str = now.strftime('%Y-%m-%d')
        filename = f"{now.strftime('%Y%m%d%H%M%S')}-Chat_{user.display_name}.md"
        
        log_parts = []
        for t in history:
            role = getattr(t, 'role', 'unknown')
            parts = getattr(t, 'parts', [])
            text = " ".join(getattr(p, 'text', '') for p in parts)
            if role in ['user', 'model'] and text: log_parts.append(f"- **{'You' if role=='user' else 'AI'}:** {text}")
        
        content = f"# Chat Log\n\n[[{date_str}]]\n\n## Review\n{review}\n\n## Transcript\n" + "\n".join(log_parts)
        
        # Folder
        log_folder = await loop.run_in_executor(None, self._find_file_recursive, service, self.drive_folder_id, ENGLISH_LOG_FOLDER)
        # Create if not exists (omitted for brevity, similar to other cogs)
        
        if log_folder:
            await loop.run_in_executor(None, self._create_text, service, log_folder, filename, content)

        # Daily Note
        daily_folder = await loop.run_in_executor(None, self._find_file_recursive, service, self.drive_folder_id, "DailyNotes")
        d_file = await loop.run_in_executor(None, self._find_file_recursive, service, daily_folder, f"{date_str}.md")
        cur = ""
        if d_file: cur = await loop.run_in_executor(None, self._read_text, service, d_file)
        
        new = update_section(cur, f"- [[{ENGLISH_LOG_FOLDER}/{filename}|Chat Log]]", "## English Logs")
        if d_file: await loop.run_in_executor(None, self._update_text, service, d_file, new)

async def setup(bot): await bot.add_cog(EnglishLearningCog(bot))