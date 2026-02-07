import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import asyncio
import datetime
import zoneinfo
import json
import google.generativeai as genai

# Google API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
import io

try: from web_parser import parse_url_with_readability
except ImportError: parse_url_with_readability = None

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
BOT_PROCESS_TRIGGER_REACTION = '📥'
RECIPE_INDEX_FILE = "recipe_index.json"
BOT_FOLDER = ".bot"
RECIPES_FOLDER = "Recipes"

SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

class RecipeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.channel_id = int(os.getenv("RECIPE_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        if self.gemini_api_key:
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")

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

    def _upload_text(self, service, parent_id, name, content):
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')
        service.files().create(body={'name': name, 'parents': [parent_id], 'mimeType': 'text/markdown'}, media_body=media).execute()

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

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.channel.id != self.channel_id: return
        if "http" in message.content: await message.add_reaction(BOT_PROCESS_TRIGGER_REACTION)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.channel_id != self.channel_id or str(payload.emoji) != BOT_PROCESS_TRIGGER_REACTION: return
        if payload.user_id != self.bot.user.id: return
        channel = self.bot.get_channel(payload.channel_id)
        msg = await channel.fetch_message(payload.message_id)
        await self._process_recipe(msg)

    async def _process_recipe(self, message):
        url = message.content.strip()
        # ... (Fetch & Extract Logic same as original) ...
        # Assume data extracted
        data = {"title": "Test Recipe", "source": url, "tags": [], "ingredients": [], "instructions": []}
        filename = f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}-Test.md"

        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        
        # Save Markdown
        r_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, RECIPES_FOLDER)
        if not r_folder: r_folder = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, RECIPES_FOLDER)
        
        content = f"# {data['title']}\nSource: {url}"
        await loop.run_in_executor(None, self._upload_text, service, r_folder, filename, content)

        # Update Index
        b_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, BOT_FOLDER)
        if not b_folder: b_folder = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, BOT_FOLDER)
        
        idx_file = await loop.run_in_executor(None, self._find_file, service, b_folder, RECIPE_INDEX_FILE)
        index = []
        if idx_file: index = await loop.run_in_executor(None, self._read_json, service, idx_file)
        
        index.insert(0, {"title": data['title'], "filename": filename})
        await loop.run_in_executor(None, self._write_json, service, b_folder, RECIPE_INDEX_FILE, index, idx_file)

        await message.reply("✅ Saved!")

async def setup(bot): await bot.add_cog(RecipeCog(bot))