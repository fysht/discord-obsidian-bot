import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import asyncio
import datetime
import zoneinfo
import aiohttp
import urllib.parse
import openai
# --- 新しいライブラリ ---
from google import genai
# ----------------------
from PIL import Image
import io
import pathlib
import json

# Google Drive API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.error("BookCog: utils/obsidian_utils.pyが見つかりません。")
    def update_section(content, text, header): return f"{content}\n\n{header}\n{text}"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
READING_NOTES_FOLDER = "Reading Notes"
BOOK_INDEX_FILE = "book_index.json"
BOT_FOLDER = ".bot"
DAILY_NOTE_SECTION = "## Reading Notes"

SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

# --- UI Components (省略) ---
# ※ 前回の回答と同様、UIコンポーネントクラス群はそのまま使用します。
# ※ 以下では省略し、BookCog本体のみ提示します。

class BookCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = int(os.getenv("BOOK_NOTE_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.google_books_api_key = os.getenv("GOOGLE_BOOKS_API_KEY")
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        self.session = aiohttp.ClientSession()
        self.openai_client = openai.AsyncOpenAI(api_key=self.openai_api_key)
        
        # --- Client初期化 ---
        if self.gemini_api_key:
            self.gemini_client = genai.Client(api_key=self.gemini_api_key)
        else:
            self.gemini_client = None
        # ------------------
        
        self.is_ready = bool(self.drive_folder_id)

    # ... (Helper methods are same: _get_drive_service, _find_file, etc.) ...
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

    def _create_folder(self, service, parent_id, name):
        f = service.files().create(body={'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}, fields='id').execute()
        return f.get('id')

    def _read_json(self, service, file_id):
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=file_id))
        done=False
        while not done: _, done = downloader.next_chunk()
        return json.loads(fh.getvalue().decode('utf-8'))

    def _write_json(self, service, parent_id, name, data, file_id=None):
        media = MediaIoBaseUpload(io.BytesIO(json.dumps(data, ensure_ascii=False).encode('utf-8')), mimetype='application/json')
        if file_id: service.files().update(fileId=file_id, media_body=media).execute()
        else: service.files().create(body={'name': name, 'parents': [parent_id]}, media_body=media).execute()

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

    async def get_book_list(self):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return [], "Error"
        
        r_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, READING_NOTES_FOLDER)
        if not r_folder: return [], None
        
        res = await loop.run_in_executor(None, lambda: service.files().list(q=f"'{r_folder}' in parents and mimeType = 'text/markdown' and trashed = false", fields="files(id, name)").execute())
        files = []
        for f in res.get('files', []):
            files.append(type('obj', (object,), {'name': f['name'], 'path_display': f['id']})) 
        return files, None

    async def _save_note_to_obsidian(self, book_data, source_url, embed_image_url_fallback=None):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return False

        title = book_data.get("title", "Untitled")
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)
        filename = f"{safe_title}.md"
        
        r_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, READING_NOTES_FOLDER)
        if not r_folder: r_folder = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, READING_NOTES_FOLDER)

        existing = await loop.run_in_executor(None, self._find_file, service, r_folder, filename)
        if existing: return "EXISTS"

        thumbnail = book_data.get("imageLinks", {}).get("thumbnail", embed_image_url_fallback or "")
        now = datetime.datetime.now(JST)
        content = f"""---
title: "{title}"
authors: {json.dumps(book_data.get('authors', []), ensure_ascii=False)}
published: {book_data.get('publishedDate', 'N/A')}
source: {source_url}
tags: [book]
status: "To Read"
created: {now.isoformat()}
cover: "{thumbnail}"
---
## Summary
{book_data.get('description', 'N/A')}

## Notes

## Actions
"""
        await loop.run_in_executor(None, self._create_text, service, r_folder, filename, content)
        
        b_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, BOT_FOLDER)
        if not b_folder: b_folder = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, BOT_FOLDER)
        
        idx_file = await loop.run_in_executor(None, self._find_file, service, b_folder, BOOK_INDEX_FILE)
        index = []
        if idx_file: index = await loop.run_in_executor(None, self._read_json, service, idx_file)
        
        index.insert(0, {
            "title": title, "authors": book_data.get('authors', []), "filename": filename,
            "status": "To Read", "cover": thumbnail, "source": book_data.get('infoLink', ''),
            "added_at": now.isoformat()
        })
        await loop.run_in_executor(None, self._write_json, service, b_folder, BOOK_INDEX_FILE, index, idx_file)
        
        daily_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, "DailyNotes")
        if not daily_folder: daily_folder = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, "DailyNotes")
        
        d_filename = f"{now.strftime('%Y-%m-%d')}.md"
        d_file = await loop.run_in_executor(None, self._find_file, service, daily_folder, d_filename)
        cur = ""
        if d_file: cur = await loop.run_in_executor(None, self._read_text, service, d_file)
        else: cur = f"# Daily Note {now.strftime('%Y-%m-%d')}\n"
        
        new = update_section(cur, f"- [ ] 📚 Start reading: [[{safe_title}]]", DAILY_NOTE_SECTION)
        if d_file: await loop.run_in_executor(None, self._update_text, service, d_file, new)
        else: await loop.run_in_executor(None, self._create_text, service, daily_folder, d_filename, new)
        
        return True

    async def _update_book_status(self, book_path, new_status):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return False

        try:
            content = await loop.run_in_executor(None, self._read_text, service, book_path)
            new_content = re.sub(r'status: ".*?"', f'status: "{new_status}"', content, count=1)
            await loop.run_in_executor(None, self._update_text, service, book_path, new_content)
            
            meta = await loop.run_in_executor(None, lambda: service.files().get(fileId=book_path, fields='name').execute())
            filename = meta['name']
            
            b_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, BOT_FOLDER)
            idx_file = await loop.run_in_executor(None, self._find_file, service, b_folder, BOOK_INDEX_FILE)
            if idx_file:
                index = await loop.run_in_executor(None, self._read_json, service, idx_file)
                for b in index:
                    if b['filename'] == filename: b['status'] = new_status; break
                await loop.run_in_executor(None, self._write_json, service, b_folder, BOOK_INDEX_FILE, index, idx_file)
            return True
        except: return False

    async def _get_current_status(self, book_path):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        try:
            content = await loop.run_in_executor(None, self._read_text, service, book_path)
            m = re.search(r'status: "(.*?)"', content)
            return m.group(1) if m else "To Read"
        except: return "To Read"

    async def _save_memo_to_obsidian_and_cleanup(self, interaction, book_path, final_text, input_type, original_message, confirmation_message):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        
        content = await loop.run_in_executor(None, self._read_text, service, book_path)
        now = datetime.datetime.now(JST)
        line = f"- {now.strftime('%Y-%m-%d %H:%M')} ({input_type}) {final_text}"
        new = update_section(content, line, "## Notes")
        await loop.run_in_executor(None, self._update_text, service, book_path, new)
        
        await confirmation_message.edit(content=f"✅ **保存済みメモ:**\n>>> {final_text}", view=None)
        # Assuming original_message passed is a discord.Message object
        try:
            # PROCESS_START_EMOJI / COMPLETE needs to be imported or defined if not global
            # For simplicity assuming they are or using hardcoded
            await original_message.remove_reaction('⏳', self.bot.user)
            await original_message.add_reaction('✅')
        except: pass

    async def _fetch_google_book_data(self, title):
        if not self.google_books_api_key or not self.session: return None
        q = urllib.parse.quote_plus(title)
        url = f"https://www.googleapis.com/books/v1/volumes?q={q}&key={self.google_books_api_key}&maxResults=5&langRestrict=ja"
        try:
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return [item['volumeInfo'] for item in data.get('items', [])]
        except: pass
        return None

async def setup(bot): await bot.add_cog(BookCog(bot))