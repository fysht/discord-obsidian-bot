import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import asyncio
import datetime
import zoneinfo
import io
import json

# Google API Imports
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError

# web_parser
from web_parser import parse_url_with_readability

# utils
try:
    from utils.obsidian_utils import update_section
except ImportError:
    def update_section(content, text, header): return f"{content}\n\n{header}\n{text}"

# --- 定数定義 ---
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
WEBCLIP_SECTION = "## WebClips"
BOT_PROCESS_TRIGGER_REACTION = '📥' 
PROCESS_START_EMOJI = '⏳'
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'

# Google Drive 設定
SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

class WebClipCog(commands.Cog):
    """ウェブページの内容を取得し、Google Drive (Obsidian) に保存するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.web_clip_channel_id = int(os.getenv("WEB_CLIP_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        
    def _get_drive_service(self):
        """Drive APIサービスを取得 (トークンリフレッシュ対応)"""
        creds = None
        if os.path.exists(TOKEN_FILE):
            try:
                creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            except: pass
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    with open(TOKEN_FILE, 'w') as token: token.write(creds.to_json())
                except: return None
            else:
                return None
        
        return build('drive', 'v3', credentials=creds)

    def _find_file(self, service, parent_id, name, mime_type=None):
        q = f"'{parent_id}' in parents and name = '{name}' and trashed = false"
        if mime_type: q += f" and mimeType = '{mime_type}'"
        res = service.files().list(q=q, fields="files(id)").execute()
        files = res.get('files', [])
        return files[0]['id'] if files else None

    def _create_folder(self, service, parent_id, name):
        meta = {'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
        file = service.files().create(body=meta, fields='id').execute()
        return file.get('id')

    def _upload_text(self, service, parent_id, name, content, file_id=None):
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown', resumable=True)
        if file_id:
            service.files().update(fileId=file_id, media_body=media).execute()
            return file_id
        else:
            meta = {'name': name, 'parents': [parent_id], 'mimeType': 'text/markdown'}
            file = service.files().create(body=meta, media_body=media, fields='id').execute()
            return file.get('id')
            
    def _read_text(self, service, file_id):
        req = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, req)
        done = False
        while done is False: _, done = downloader.next_chunk()
        return fh.getvalue().decode('utf-8')

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        if message.channel.id != self.web_clip_channel_id: return
        if URL_REGEX.search(message.content):
            await message.add_reaction(BOT_PROCESS_TRIGGER_REACTION)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.channel_id != self.web_clip_channel_id: return
        if str(payload.emoji) != BOT_PROCESS_TRIGGER_REACTION: return
        if payload.user_id != self.bot.user.id: return

        channel = self.bot.get_channel(payload.channel_id)
        try: message = await channel.fetch_message(payload.message_id)
        except: return

        if any(r.emoji in (PROCESS_START_EMOJI, PROCESS_COMPLETE_EMOJI) and r.me for r in message.reactions): return
        try: await message.remove_reaction(payload.emoji, self.bot.user)
        except: pass

        await self._perform_clip(message)

    async def _perform_clip(self, message: discord.Message):
        if not self.drive_folder_id:
            logging.error("GOOGLE_DRIVE_FOLDER_ID not set")
            await message.add_reaction(PROCESS_ERROR_EMOJI)
            return

        url = message.content.strip()
        try:
            await message.add_reaction(PROCESS_START_EMOJI)
            
            # 1. コンテンツ取得
            loop = asyncio.get_running_loop()
            parsed_title, content_md = await loop.run_in_executor(None, parse_url_with_readability, url)
            
            title = message.embeds[0].title if message.embeds and message.embeds[0].title else parsed_title
            if not title or title == "No Title Found": title = "Untitled"
            
            safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
            now = datetime.datetime.now(JST)
            timestamp = now.strftime('%Y%m%d%H%M%S')
            date_str = now.strftime('%Y-%m-%d')
            
            file_name = f"{timestamp}-{safe_title}.md"
            note_content = f"# {title}\n\n- **Source:** <{url}>\n---\n[[{date_str}]]\n\n{content_md}"

            # 2. Drive操作
            service = await loop.run_in_executor(None, self._get_drive_service)
            if not service: raise Exception("Drive Service Init Failed")

            # WebClipsフォルダ確認
            clips_folder_id = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, "WebClips", "application/vnd.google-apps.folder")
            if not clips_folder_id:
                clips_folder_id = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, "WebClips")

            # 個別ファイル作成
            await loop.run_in_executor(None, self._upload_text, service, clips_folder_id, file_name, note_content)

            # Daily Note更新
            daily_folder_id = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, "DailyNotes", "application/vnd.google-apps.folder")
            if not daily_folder_id:
                daily_folder_id = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, "DailyNotes")
            
            daily_file_name = f"{date_str}.md"
            daily_file_id = await loop.run_in_executor(None, self._find_file, service, daily_folder_id, daily_file_name)
            
            current_daily = ""
            if daily_file_id:
                current_daily = await loop.run_in_executor(None, self._read_text, service, daily_file_id)
            else:
                current_daily = f"# Daily Note {date_str}\n"

            link_text = f"- [[WebClips/{file_name.replace('.md','')}|{title}]]"
            new_daily = update_section(current_daily, link_text, WEBCLIP_SECTION)
            
            await loop.run_in_executor(None, self._upload_text, service, daily_folder_id, daily_file_name, new_daily, daily_file_id)

            await message.add_reaction(PROCESS_COMPLETE_EMOJI)

        except Exception as e:
            logging.error(f"WebClip Error: {e}", exc_info=True)
            await message.add_reaction(PROCESS_ERROR_EMOJI)
        finally:
            try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except: pass

    @app_commands.command(name="clip", description="URLをObsidianにクリップ")
    async def clip(self, interaction: discord.Interaction, url: str):
        await interaction.response.defer()
        class MockMsg:
            def __init__(self, i, c): 
                self.id=i.id; self.channel=i.channel; self.content=c; self.embeds=[]; self.author=i.user
                self.reactions=[]
            async def add_reaction(self, e): pass
            async def remove_reaction(self, e, u): pass
        
        await self._perform_clip(MockMsg(interaction, url))
        await interaction.followup.send(f"✅ クリップ処理を開始: {url}")

async def setup(bot: commands.Bot):
    await bot.add_cog(WebClipCog(bot))