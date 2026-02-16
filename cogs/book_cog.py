import os
import discord
from discord.ext import commands
import datetime
import zoneinfo
import io
import aiohttp
import re
import asyncio

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
TOKEN_FILE = 'token.json'
SCOPES = ['https://www.googleapis.com/auth/drive']

class BookCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

    def get_drive_service(self):
        creds = None
        if os.path.exists(TOKEN_FILE): 
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try: 
                    creds.refresh(Request())
                    open(TOKEN_FILE,'w').write(creds.to_json())
                except: 
                    return None
            else: return None
        return build('drive', 'v3', credentials=creds)

    async def _find_file(self, service, parent_id, name, mime_type=None):
        loop = asyncio.get_running_loop()
        query = f"'{parent_id}' in parents and name = '{name}' and trashed = false"
        if mime_type:
            query += f" and mimeType = '{mime_type}'"
        try:
            res = await loop.run_in_executor(None, lambda: service.files().list(q=query, fields="files(id)").execute())
            files = res.get('files', [])
            return files[0]['id'] if files else None
        except: return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Bot自身の投稿や、指定チャンネル以外は無視
        if message.author.bot or message.channel.id != self.memo_channel_id:
            return

        text = message.content.strip()

        # AmazonのURLが含まれているか正規表現でチェック（amzn.to や amazon.co.jp）
        amazon_pattern = r'(https?://(?:www\.)?(?:amazon\.co\.jp|amzn\.to)[^\s]+)'
        match = re.search(amazon_pattern, text)
        
        if match:
            url = match.group(1)
            # 処理に数秒かかるため、反応したことを知らせるリアクションをつける
            await message.add_reaction("📚")
            # ノートとスレッドの作成処理を非同期で走らせる
            asyncio.create_task(self.process_book_link(message, url))

    async def process_book_link(self, message: discord.Message, url: str):
        title = "名称未設定の本"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    html = await resp.text()
                    match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
                    if match:
                        # Amazon特有の不要な文字列を削除して綺麗にする
                        title = match.group(1).replace("Amazon.co.jp:", "").replace("Amazon.co.jp :", "").strip()
        except Exception as e:
            pass 

        # ファイル名として使えない記号を置換
        safe_title = re.sub(r'[\\/*?:"<>|]', '_', title)[:50]

        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self.get_drive_service)
        if service:
            # 1. BookNotesフォルダを探す（なければ作成）
            book_folder_id = await self._find_file(service, self.drive_folder_id, "BookNotes", "application/vnd.google-apps.folder")
            if not book_folder_id:
                meta = {'name': "BookNotes", 'mimeType': 'application/vnd.google-apps.folder', 'parents': [self.drive_folder_id]}
                folder_obj = await loop.run_in_executor(None, lambda: service.files().create(body=meta, fields='id').execute())
                book_folder_id = folder_obj.get('id')

            # 2. 書籍のノート（.mdファイル）を作成
            file_name = f"{safe_title}.md"
            f_id = await self._find_file(service, book_folder_id, file_name)
            if not f_id:
                now_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
                content = f"---\ntitle: {safe_title}\ndate: {now_str}\ntags: [book]\n---\n\n# {safe_title}\n\n## 📝 要約・学び\n\n\n## 💬 読書ログ\n\n"
                media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')
                await loop.run_in_executor(None, lambda: service.files().create(body={'name': file_name, 'parents': [book_folder_id]}, media_body=media).execute())

        # 3. Discordにメッセージを返信し、スレッドを作成
        msg = await message.reply(f"📚 『{safe_title}』の読書ノートを作成したよ！\nこのスレッドでメモや感想を書いてね。")
        thread = await msg.create_thread(name=f"📖 {safe_title}", auto_archive_duration=10080)
        await thread.send("ここが読書ルームだよ！気軽にメモしたり、わからないことをAIに質問してね。")

async def setup(bot: commands.Bot):
    await bot.add_cog(BookCog(bot))