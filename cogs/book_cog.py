import os
import discord
from discord import app_commands
from discord.ext import commands
import datetime
import zoneinfo
import io
import aiohttp
import re
import asyncio
from google import genai

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
TOKEN_FILE = 'token.json'
SCOPES = ['https://www.googleapis.com/auth/drive']

class BookCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

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
        if message.author.bot or message.channel.id != self.memo_channel_id:
            return

        text = message.content.strip()

        amazon_pattern = r'(https?://(?:www\.)?(?:amazon\.co\.jp|amzn\.to)[^\s]+)'
        match = re.search(amazon_pattern, text)
        
        if match:
            url = match.group(1)
            await message.add_reaction("📚")
            asyncio.create_task(self.process_book_link(message, url))

    async def process_book_link(self, message: discord.Message, url: str):
        title = "名称未設定の本"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    html = await resp.text()
                    match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
                    if match:
                        title = match.group(1).replace("Amazon.co.jp:", "").replace("Amazon.co.jp :", "").strip()
        except Exception as e:
            pass 

        safe_title = re.sub(r'[\\/*?:"<>|]', '_', title)[:50]

        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self.get_drive_service)
        if service:
            book_folder_id = await self._find_file(service, self.drive_folder_id, "BookNotes", "application/vnd.google-apps.folder")
            if not book_folder_id:
                meta = {'name': "BookNotes", 'mimeType': 'application/vnd.google-apps.folder', 'parents': [self.drive_folder_id]}
                folder_obj = await loop.run_in_executor(None, lambda: service.files().create(body=meta, fields='id').execute())
                book_folder_id = folder_obj.get('id')

            file_name = f"{safe_title}.md"
            f_id = await self._find_file(service, book_folder_id, file_name)
            if not f_id:
                now_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
                content = f"---\ntitle: {safe_title}\ndate: {now_str}\ntags: [book]\n---\n\n# {safe_title}\n\n## 📝 要約・学び\n\n\n## 💬 読書ログ\n\n"
                media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')
                await loop.run_in_executor(None, lambda: service.files().create(body={'name': file_name, 'parents': [book_folder_id]}, media_body=media).execute())

        msg = await message.reply(f"📚 『{safe_title}』の読書ノートを作成したよ！\nこのスレッドでメモや感想を書いてね。")
        thread = await msg.create_thread(name=f"📖 {safe_title}", auto_archive_duration=10080)
        await thread.send("ここが読書ルームだよ！気軽にメモしたり、わからないことをAIに質問してね。\nまとめを作りたくなったら `/summarize_book` を実行してね。")

    @app_commands.command(name="summarize_book", description="現在の読書スレッドのログをAIが整理し、ノートの要約を更新します")
    async def summarize_book(self, interaction: discord.Interaction):
        # スレッド内でのみ実行可能にする
        if not isinstance(interaction.channel, discord.Thread) or not interaction.channel.name.startswith("📖 "):
            await interaction.response.send_message("このコマンドは「📖」から始まる読書スレッドの中でのみ実行できるよ！", ephemeral=True)
            return

        await interaction.response.defer()
        book_title = interaction.channel.name[2:].strip()
        file_name = f"{book_title}.md"

        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self.get_drive_service)
        if not service:
            await interaction.followup.send("Google Driveに接続できなかったよ💦")
            return

        # BookNotesフォルダと対象ファイルを探す
        book_folder_id = await self._find_file(service, self.drive_folder_id, "BookNotes", "application/vnd.google-apps.folder")
        if not book_folder_id:
            await interaction.followup.send("BookNotesフォルダが見つからないみたい。")
            return

        f_id = await self._find_file(service, book_folder_id, file_name)
        if not f_id:
            await interaction.followup.send(f"ノート（{file_name}）が見つからないよ。")
            return

        # ファイルの中身を読み込む
        try:
            request = service.files().get_media(fileId=f_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: _, done = downloader.next_chunk()
            content = fh.getvalue().decode('utf-8')
        except Exception as e:
            await interaction.followup.send(f"ノートの読み込みに失敗したよ: {e}")
            return

        # 「読書ログ」部分を抽出する
        log_heading = "## 💬 読書ログ"
        summary_heading = "## 📝 要約・学び"
        
        if log_heading not in content or summary_heading not in content:
            await interaction.followup.send("ノートの形式が正しくないみたい（見出しが見つかりません）。")
            return

        parts = content.split(log_heading)
        top_half = parts[0].split(summary_heading)[0] # 要約見出しより上の部分（フロントマターなど）
        raw_log = parts[1].strip()

        if not raw_log:
            await interaction.followup.send("まだ読書ログがないみたいだよ！")
            return

        # Geminiにログを渡して要約させる
        prompt = f"""
        あなたは優秀な編集者です。以下の「読書ログ（ユーザーのメモやAIとの会話）」を読み込み、構造化された美しいまとめを作成してください。
        
        【出力ルール】
        - 以下の3つの見出し（Markdownの h3）を必ず含め、箇条書きで簡潔に整理すること。
          ### 📌 重要な引用・ハイライト
          ### 💡 気づき・学び
          ### 🤖 AIの解説・用語メモ
        - 余計な前置きや後書き（「まとめました」など）は一切出力せず、指定した見出しの内容のみを出力すること。

        【読書ログ】
        {raw_log}
        """

        try:
            response = await self.gemini_client.aio.models.generate_content(model="gemini-2.5-pro", contents=prompt)
            summary_text = response.text.strip()
            
            # 新しい内容でファイルを組み立て直す
            new_content = f"{top_half}{summary_heading}\n{summary_text}\n\n\n{log_heading}\n{raw_log}\n"
            
            media = MediaIoBaseUpload(io.BytesIO(new_content.encode('utf-8')), mimetype='text/markdown')
            await loop.run_in_executor(None, lambda: service.files().update(fileId=f_id, media_body=media).execute())
            
            await interaction.followup.send("✨ 読書ノートの「要約・学び」セクションを綺麗に整理してObsidianに保存したよ！")

        except Exception as e:
            await interaction.followup.send(f"AIの要約中にエラーが発生したよ💦: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(BookCog(bot))