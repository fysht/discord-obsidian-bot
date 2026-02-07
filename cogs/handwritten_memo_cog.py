import discord
from discord.ext import commands
import os
import aiohttp
import asyncio
import json
import logging
import re
from datetime import datetime
import google.generativeai as genai
import io

# Google Drive API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

from utils.obsidian_utils import update_section

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# Google Drive 設定
SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

class HandwrittenMemo(commands.Cog):
    """手書きメモ(PDF/画像)を解析し、Google Drive (Obsidian) に保存するCog"""

    def __init__(self, bot):
        self.bot = bot
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.attachment_folder_name = "99_Attachments"

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

    def _upload_file(self, service, parent_id, name, data, mime_type):
        meta = {'name': name, 'parents': [parent_id]}
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=True)
        file = service.files().create(body=meta, media_body=media, fields='id').execute()
        return file.get('id')

    def _read_text(self, service, file_id):
        req = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, req)
        done = False
        while done is False: _, done = downloader.next_chunk()
        return fh.getvalue().decode('utf-8')

    def _update_text(self, service, file_id, content):
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown', resumable=True)
        service.files().update(fileId=file_id, media_body=media).execute()

    def _create_text(self, service, parent_id, name, content):
        meta = {'name': name, 'parents': [parent_id], 'mimeType': 'text/markdown'}
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown', resumable=True)
        service.files().create(body=meta, media_body=media).execute()

    async def analyze_memo_content(self, file_bytes, mime_type):
        try:
            model = genai.GenerativeModel('gemini-2.5-pro') 
            prompt = "画像の手書きメモから日付(YYYY-MM-DD)と内容のMarkdown箇条書きを抽出してJSONで返して。{'date':..., 'content':...}"
            file_part = {"mime_type": mime_type, "data": file_bytes}
            response = await model.generate_content_async([prompt, file_part])
            text = response.text.strip()
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                res = json.loads(match.group(0))
                return res.get("date"), res.get("content")
            return None, text
        except Exception as e:
            logging.error(f"Analysis error: {e}")
            return None, None

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot: return
        if message.attachments:
            for attachment in message.attachments:
                if any(attachment.content_type.startswith(t) for t in ['image/', 'application/pdf']):
                    await self.process_scanned_file(message, attachment)
                    return

    async def process_scanned_file(self, message, attachment):
        processing_msg = await message.channel.send("🔄 手書きメモ解析中...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(attachment.url) as resp:
                    if resp.status != 200: raise Exception("DL失敗")
                    file_bytes = await resp.read()
                    mime_type = attachment.content_type

            date_str, content = await self.analyze_memo_content(file_bytes, mime_type)
            if not date_str: date_str = datetime.now().strftime('%Y-%m-%d')
            if not content: content = "解析失敗"

            loop = asyncio.get_running_loop()
            service = await loop.run_in_executor(None, self._get_drive_service)
            if not service: raise Exception("Drive接続失敗")

            # 1. 添付ファイル保存
            att_folder_id = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, self.attachment_folder_name, "application/vnd.google-apps.folder")
            if not att_folder_id:
                att_folder_id = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, self.attachment_folder_name)
            
            saved_filename = f"Scan_{date_str}_{datetime.now().strftime('%H%M%S')}_{attachment.filename}"
            await loop.run_in_executor(None, self._upload_file, service, att_folder_id, saved_filename, file_bytes, mime_type)

            # 2. Daily Note更新
            daily_folder_id = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, "DailyNotes")
            if not daily_folder_id:
                 daily_folder_id = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, "DailyNotes")

            daily_file_name = f"{date_str}.md"
            daily_file_id = await loop.run_in_executor(None, self._find_file, service, daily_folder_id, daily_file_name)
            
            current_text = ""
            if daily_file_id:
                current_text = await loop.run_in_executor(None, self._read_text, service, daily_file_id)
            
            timestamp = datetime.now().strftime('%H:%M')
            to_add = f"- {timestamp} (Handwritten)\n\t- ![[{self.attachment_folder_name}/{saved_filename}]]\n"
            for line in content.split('\n'):
                to_add += f"\t- {line}\n"
            
            new_text = update_section(current_text, to_add, "## Handwritten Memos")
            
            if daily_file_id:
                await loop.run_in_executor(None, self._update_text, service, daily_file_id, new_text)
            else:
                await loop.run_in_executor(None, self._create_text, service, daily_folder_id, daily_file_name, new_text)

            embed = discord.Embed(title=f"📝 保存完了 ({date_str})", description=content, color=discord.Color.green())
            await processing_msg.edit(content="", embed=embed)
            await message.add_reaction("✅")

        except Exception as e:
            logging.error(f"Error: {e}")
            await processing_msg.edit(content=f"❌ エラー: {e}")

async def setup(bot):
    await bot.add_cog(HandwrittenMemo(bot))