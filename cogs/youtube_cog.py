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
import aiohttp

# Google API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

try:
    from utils.obsidian_utils import update_section
except ImportError:
    def update_section(content, text, header): return f"{content}\n\n{header}\n{text}"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})')
SECTION_HEADER = "## YouTube Summaries"

SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

class YouTubeCog(commands.Cog):
    """
    YouTube動画の投稿を検知し、その内容をObsidian(Google Drive)に自動保存するCog
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.youtube_summary_channel_id = int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID") # Vault Root
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        await self.session.close()

    def _get_drive_service(self):
        creds = None
        if os.path.exists(TOKEN_FILE):
            try: creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            except: pass
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    with open(TOKEN_FILE, 'w') as token: token.write(creds.to_json())
                except: return None
            else: return None
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

    # --- メッセージ受信時の自動処理 ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Bot自身の投稿や、対象外のチャンネルは無視
        if message.author.bot: return
        if message.channel.id != self.youtube_summary_channel_id: return

        # YouTubeのURLが含まれているかチェック
        if YOUTUBE_URL_REGEX.search(message.content):
            await self._perform_save(message)

    # --- (オプション) 手動リアクションでの保存 ---
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.channel_id != self.youtube_summary_channel_id: return
        if payload.user_id == self.bot.user.id: return
        
        # 特定の絵文字（例えば 📥 ）が押された場合のみ手動実行
        if str(payload.emoji) == '📥':
            channel = self.bot.get_channel(payload.channel_id)
            if not channel: return
            try:
                message = await channel.fetch_message(payload.message_id)
                await self._perform_save(message)
            except: return

    async def _perform_save(self, message: discord.Message):
        if not self.drive_folder_id:
            try: await message.add_reaction("❌")
            except: pass
            logging.error("GOOGLE_DRIVE_FOLDER_ID not set")
            return

        url = message.content.strip()
        video_id_match = YOUTUBE_URL_REGEX.search(url)
        if not video_id_match:
            return # URLがない場合は無視
        
        video_id = video_id_match.group(1)

        try:
            # 処理開始リアクション
            try: await message.add_reaction("⏳")
            except: pass

            # 1. 動画情報取得 (タイトル等)
            video_info = await self.get_video_info(video_id)
            video_title = video_info.get("title", "No Title")
            
            # 2. ファイル作成準備
            now = datetime.datetime.now(JST)
            date_str = now.strftime('%Y-%m-%d')
            timestamp = now.strftime('%Y%m%d%H%M%S')
            
            # ファイル名に使えない文字を除去
            safe_title = re.sub(r'[\\/*?:"<>|]', "", video_title)
            # ファイル名が長すぎるとエラーになる場合があるので制限
            safe_title = safe_title[:50]
            note_filename = f"{timestamp}-{safe_title}.md"
            
            # ノート本文の作成: 投稿内容をそのまま保存
            note_content = (
                f"# {video_title}\n\n"
                f'<iframe width="560" height="315" src="https://www.youtube.com/embed/{video_id}" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>\n\n'
                f"- **URL:** https://www.youtube.com/watch?v={video_id}\n"
                f"- **Date:** [[{date_str}]]\n"
                f"- **Posted by:** {message.author.display_name}\n\n"
                f"---\n\n"
                f"{message.content}\n"
            )

            # 3. Google Drive操作 (別スレッドで実行)
            loop = asyncio.get_running_loop()
            service = await loop.run_in_executor(None, self._get_drive_service)
            if not service: raise Exception("Drive Service Init Failed")

            # YouTubeフォルダ確認・作成
            yt_folder_id = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, "YouTube", "application/vnd.google-apps.folder")
            if not yt_folder_id:
                yt_folder_id = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, "YouTube")

            # 個別ファイル保存
            await loop.run_in_executor(None, self._upload_text, service, yt_folder_id, note_filename, note_content)

            # デイリーノート更新
            daily_folder_id = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, "DailyNotes", "application/vnd.google-apps.folder")
            if not daily_folder_id:
                # DailyNotesフォルダがない場合は作成
                daily_folder_id = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, "DailyNotes")
            
            daily_file_name = f"{date_str}.md"
            daily_file_id = await loop.run_in_executor(None, self._find_file, service, daily_folder_id, daily_file_name)
            
            if daily_file_id:
                current_daily = await loop.run_in_executor(None, self._read_text, service, daily_file_id)
            else:
                current_daily = f"# Daily Note {date_str}\n"

            link_text = f"- [[YouTube/{note_filename.replace('.md', '')}|{video_title}]]"
            new_daily = update_section(current_daily, link_text, SECTION_HEADER)
            
            # デイリーノート保存（更新または新規作成）
            await loop.run_in_executor(None, self._upload_text, service, daily_folder_id, daily_file_name, new_daily, daily_file_id)

            # 完了リアクション
            try:
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("✅")
            except: pass

        except Exception as e:
            logging.error(f"YouTube process error: {e}", exc_info=True)
            try:
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("❌")
            except: pass

    async def get_video_info(self, video_id: str) -> dict:
        url = f"https://www.youtube.com/oembed?url=http://www.youtube.com/watch?v={video_id}&format=json"
        try:
            async with self.session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return {"title": data.get("title"), "author_name": data.get("author_name")}
        except: pass
        return {"title": f"YouTube-{video_id}", "author_name": "N/A"}

async def setup(bot: commands.Bot):
    await bot.add_cog(YouTubeCog(bot))