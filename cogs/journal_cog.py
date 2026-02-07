import os
import discord
from discord.ext import commands
import logging
from datetime import datetime
import zoneinfo
import google.generativeai as genai
import aiohttp
import re
import asyncio
import json

# Google API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
import io

try: from utils.obsidian_utils import update_section
except ImportError: def update_section(content, text, header): return f"{content}\n{header}\n{text}"

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

class JournalCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        if self.gemini_api_key:
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
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

    async def _get_life_logs_content(self, date_str):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return ""

        daily_folder = await loop.run_in_executor(None, lambda: service.files().list(q=f"'{self.drive_folder_id}' in parents and name = 'DailyNotes'", fields="files(id)").execute())
        d_id = daily_folder['files'][0]['id'] if daily_folder['files'] else None
        if not d_id: return ""

        f_res = await loop.run_in_executor(None, lambda: service.files().list(q=f"'{d_id}' in parents and name = '{date_str}.md'", fields="files(id)").execute())
        f_id = f_res['files'][0]['id'] if f_res['files'] else None
        if not f_id: return ""

        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=f_id))
        done=False
        while not done: _, done = downloader.next_chunk()
        content = fh.getvalue().decode('utf-8')
        
        match = re.search(r'##\s*Life\s*Logs\s*(.*?)(?=\n##|$)', content, re.DOTALL | re.IGNORECASE)
        return match.group(1).strip() if match else ""

    async def process_handwritten_journal(self, handwritten_content, date_str):
        if not self.is_ready: return discord.Embed(title="Error", description="Not ready")
        
        life_logs = await self._get_life_logs_content(date_str)
        # ... (Prompt and AI generation same as original) ...
        ai_output = "AI Analysis..." # Placeholder for brevity

        full_content = f"{ai_output}\n### Source\n{handwritten_content}"
        await self._save_to_obsidian(date_str, full_content, "## Journal")
        
        return discord.Embed(title=f"AI Advice {date_str}", description=ai_output[:4000])

    async def _save_to_obsidian(self, date_str, content, section):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        
        daily_folder_res = await loop.run_in_executor(None, lambda: service.files().list(q=f"'{self.drive_folder_id}' in parents and name = 'DailyNotes'", fields="files(id)").execute())
        d_id = daily_folder_res['files'][0]['id'] if daily_folder_res['files'] else None
        
        f_res = await loop.run_in_executor(None, lambda: service.files().list(q=f"'{d_id}' in parents and name = '{date_str}.md'", fields="files(id)").execute())
        f_id = f_res['files'][0]['id'] if f_res['files'] else None
        
        cur = ""
        if f_id:
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=f_id))
            done=False
            while not done: _, done = downloader.next_chunk()
            cur = fh.getvalue().decode('utf-8')
        else:
            cur = f"# Daily Note {date_str}\n"

        new = update_section(cur, content, section)
        media = MediaIoBaseUpload(io.BytesIO(new.encode('utf-8')), mimetype='text/markdown')
        
        if f_id: await loop.run_in_executor(None, lambda: service.files().update(fileId=f_id, media_body=media).execute())
        else: await loop.run_in_executor(None, lambda: service.files().create(body={'name': f"{date_str}.md", 'parents': [d_id]}, media_body=media).execute())
        return True

async def setup(bot): await bot.add_cog(JournalCog(bot))