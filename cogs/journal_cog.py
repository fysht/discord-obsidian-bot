import os
import discord
from discord.ext import commands
import logging
from datetime import datetime
import zoneinfo
# --- 新しいライブラリ ---
from google import genai
# ----------------------
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

try: 
    from utils.obsidian_utils import update_section
except ImportError: 
    def update_section(content, text, header): return f"{content}\n{header}\n{text}"

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

class JournalCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        # --- Client初期化 ---
        if self.gemini_api_key:
            self.gemini_client = genai.Client(api_key=self.gemini_api_key)
        else:
            self.gemini_client = None
        # ------------------
        
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

        daily_folder_res = await loop.run_in_executor(None, lambda: service.files().list(q=f"'{self.drive_folder_id}' in parents and name = 'DailyNotes' and trashed = false", fields="files(id)").execute())
        d_id = daily_folder_res['files'][0]['id'] if daily_folder_res.get('files') else None
        if not d_id: return ""

        f_res = await loop.run_in_executor(None, lambda: service.files().list(q=f"'{d_id}' in parents and name = '{date_str}.md' and trashed = false", fields="files(id)").execute())
        f_id = f_res['files'][0]['id'] if f_res.get('files') else None
        if not f_id: return ""

        try:
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=f_id))
            done=False
            while not done: _, done = downloader.next_chunk()
            content = fh.getvalue().decode('utf-8')
            
            match = re.search(r'##\s*Life\s*Logs\s*(.*?)(?=\n##|$)', content, re.DOTALL | re.IGNORECASE)
            return match.group(1).strip() if match else ""
        except: return ""

    async def process_handwritten_journal(self, handwritten_content, date_str):
        if not self.is_ready: return discord.Embed(title="Error", description="Not ready")
        
        life_logs = await self._get_life_logs_content(date_str)
        
        prompt = f"""
        あなたは日々の記録を分析・整理するAIです。
        ユーザーが書いた「手書きの振り返り（OCR）」と、システムが記録した「ライフログ（時間記録）」を統合し、
        **今日一日の分析と、明日への具体的なアドバイス**を行ってください。

        # 情報ソース
        ## 【A】ライフログ（客観的な時間の使い方）
        {life_logs if life_logs else "(記録なし)"}
        
        ## 【B】手書きの振り返り（ユーザーの主観・思考）
        {handwritten_content}

        # 指示
        以下のフォーマットでMarkdownテキストを出力してください。
        【重要】全体を通して、文末は「である調（〜である、〜だ）」で統一してください。敬語や丁寧語は使わないでください。

        ### 1. 🤖 AI Analysis & Advice
        - **時間の使い方**: ライフログと振り返りを照らし合わせ、時間の使い方の傾向や、集中できていた点、改善できる点を客観的に指摘すること。
        - **メンタルケア**: 感情の揺れ動きを分析し、改善に向けた見解を示すこと。
        - **明日への提案**: 明日具体的に意識すべきアクションを1〜2点提案すること。

        ### 2. 📝 Daily Summary
        - 今日の出来事を箇条書きで整理すること。
        - ユーザーの記述を可能な限りすべて拾い、情報の整理はするが、要約や大幅な削除はしないこと。
        """

        try:
            if self.gemini_client:
                # --- 生成メソッド変更 ---
                response = await self.gemini_client.aio.models.generate_content(
                    model='gemini-2.5-pro',
                    contents=prompt
                )
                ai_output = response.text.strip()
            else:
                ai_output = "API Key not set."
        except Exception as e:
            ai_output = f"AI Error: {e}"

        full_content = f"{ai_output}\n\n### Source (Handwritten OCR)\n{handwritten_content}"
        await self._save_to_obsidian(date_str, full_content, "## Journal")
        
        advice_part = ai_output
        if "### 1. 🤖 AI Analysis & Advice" in ai_output:
            parts = ai_output.split("### 2. 📝 Daily Summary")
            advice_part = parts[0].replace("### 1. 🤖 AI Analysis & Advice", "").strip()

        return discord.Embed(title=f"🤖 AI Advice for {date_str}", description=advice_part[:4000], color=discord.Color.gold())

    async def _save_to_obsidian(self, date_str, content, section):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return False
        
        daily_folder_res = await loop.run_in_executor(None, lambda: service.files().list(q=f"'{self.drive_folder_id}' in parents and name = 'DailyNotes' and trashed = false", fields="files(id)").execute())
        d_id = daily_folder_res['files'][0]['id'] if daily_folder_res.get('files') else None
        
        if not d_id: return False

        f_res = await loop.run_in_executor(None, lambda: service.files().list(q=f"'{d_id}' in parents and name = '{date_str}.md' and trashed = false", fields="files(id)").execute())
        f_id = f_res['files'][0]['id'] if f_res.get('files') else None
        
        cur = ""
        if f_id:
            try:
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=f_id))
                done=False
                while not done: _, done = downloader.next_chunk()
                cur = fh.getvalue().decode('utf-8')
            except: pass
        else:
            cur = f"# Daily Note {date_str}\n"

        new = update_section(cur, content, section)
        media = MediaIoBaseUpload(io.BytesIO(new.encode('utf-8')), mimetype='text/markdown')
        
        if f_id: await loop.run_in_executor(None, lambda: service.files().update(fileId=f_id, media_body=media).execute())
        else: await loop.run_in_executor(None, lambda: service.files().create(body={'name': f"{date_str}.md", 'parents': [d_id]}, media_body=media).execute())
        return True

async def setup(bot): await bot.add_cog(JournalCog(bot))