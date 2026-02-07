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

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
MORNING_SAKUBUN_TIME = time(hour=8, minute=0, tzinfo=JST)
EVENING_SAKUBUN_TIME = time(hour=21, minute=0, tzinfo=JST)
SAKUBUN_FILE_NAME = "瞬間英作文リスト.md"
ENGLISH_LOG_FOLDER = "English Learning"
SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

class EnglishLearningCog(commands.Cog, name="EnglishLearning"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = int(os.getenv("ENGLISH_LEARNING_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        if self.gemini_api_key:
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
        
        self.chat_sessions = {}
        self.sakubun_questions = []
        self.is_ready = bool(self.drive_folder_id and self.channel_id)

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
        try:
            res = service.files().list(q=f"'{parent_id}' in parents and name = '{name}' and trashed = false", fields="files(id)").execute()
            files = res.get('files', [])
            return files[0]['id'] if files else None
        except: return None

    def _find_file_recursive(self, service, parent_id, name):
        return self._find_file(service, parent_id, name)

    def _read_text(self, service, file_id):
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=file_id))
        done=False
        while not done: _, done = downloader.next_chunk()
        return fh.getvalue().decode('utf-8')

    def _create_text(self, service, parent_id, name, content):
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')
        service.files().create(body={'name': name, 'parents': [parent_id], 'mimeType': 'text/markdown'}, media_body=media).execute()

    def _update_text(self, service, file_id, content):
        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')
        service.files().update(fileId=file_id, media_body=media).execute()

    def _create_folder(self, service, parent_id, name):
        f = service.files().create(body={'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}, fields='id').execute()
        return f.get('id')

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
        
        # "Study" フォルダを探す（なければルート直下を探すフォールバックも検討可）
        study_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, "Study")
        
        # Studyフォルダがなければルートを探す
        target_folder = study_folder if study_folder else self.drive_folder_id
        
        file_id = await loop.run_in_executor(None, self._find_file, service, target_folder, SAKUBUN_FILE_NAME)
        if file_id:
            try:
                content = await loop.run_in_executor(None, self._read_text, service, file_id)
                # 箇条書きから質問を抽出 (形式: "- 日本語 :: 英語" または "- 日本語")
                questions = []
                for line in content.split('\n'):
                    line = line.strip()
                    if line.startswith('-'):
                        clean_line = line.lstrip('- ').strip()
                        if "::" in clean_line:
                            q_part = clean_line.split("::")[0].strip()
                            questions.append(q_part)
                        else:
                            questions.append(clean_line)
                
                if questions:
                    self.sakubun_questions = questions
                    logging.info(f"Loaded {len(questions)} sakubun questions.")
            except Exception as e:
                logging.error(f"Error loading sakubun questions: {e}")

    # --- Tasks ---

    @tasks.loop(time=MORNING_SAKUBUN_TIME)
    async def morning_sakubun_task(self):
        await self._send_random_question("☀️ 朝の瞬間英作文")

    @tasks.loop(time=EVENING_SAKUBUN_TIME)
    async def evening_sakubun_task(self):
        await self._send_random_question("🌙 夜の瞬間英作文")

    async def _send_random_question(self, title):
        if not self.sakubun_questions: return
        channel = self.bot.get_channel(self.channel_id)
        if not channel: return

        question = random.choice(self.sakubun_questions)
        embed = discord.Embed(title=title, description=f"**{question}**\n\n(英語に翻訳して送信してください)", color=discord.Color.green())
        await channel.send(embed=embed)

    # --- Commands ---

    @app_commands.command(name="english", description="AIと英会話を開始します")
    async def english(self, interaction: discord.Interaction):
        if interaction.channel_id != self.channel_id:
            await interaction.response.send_message(f"<#{self.channel_id}> で実行してください。", ephemeral=True)
            return
        
        user_id = interaction.user.id
        self.chat_sessions[user_id] = [] # 履歴初期化
        
        await interaction.response.send_message("Let's start English conversation! 🇺🇸\n(Type `end` or `finish` to stop and save the log.)")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.channel.id != self.channel_id: return
        
        user_id = message.author.id
        if user_id in self.chat_sessions:
            content = message.content.strip()
            
            # 終了判定
            if content.lower() in ["end", "finish", "quit", "終了"]:
                await message.channel.send("Conversation ended. Saving log...")
                await self._finish_session(message.author)
                return

            # AI応答
            self.chat_sessions[user_id].append({"role": "user", "parts": [content]})
            async with message.channel.typing():
                try:
                    chat = self.gemini_model.start_chat(history=self.chat_sessions[user_id][:-1])
                    response = await chat.send_message_async(content)
                    ai_text = response.text
                    
                    self.chat_sessions[user_id].append({"role": "model", "parts": [ai_text]})
                    await message.channel.send(ai_text)
                except Exception as e:
                    await message.channel.send(f"⚠️ Error: {e}")

    async def _finish_session(self, user):
        user_id = user.id
        history = self.chat_sessions.get(user_id, [])
        if not history:
            del self.chat_sessions[user_id]
            return

        # 要約とアドバイス生成
        full_text = "\n".join([f"{h['role']}: {h['parts'][0]}" for h in history])
        prompt = f"""
        以下の英会話ログを分析し、以下の項目を出力してください。
        1. **Summary**: 会話の内容の要約 (日本語)
        2. **Corrections**: ユーザーの英語の誤りと、より自然な表現の提案 (箇条書き)
        3. **Advice**: 今後の学習アドバイス

        --- Log ---
        {full_text}
        """
        try:
            res = await self.gemini_model.generate_content_async(prompt)
            review = res.text
        except: review = "Review generation failed."

        # Google Drive (Obsidian) に保存
        await self._save_chat_log_to_obsidian(user, history, review)
        
        del self.chat_sessions[user_id]
        
        embed = discord.Embed(title="📝 Conversation Review", description=review[:4000], color=discord.Color.blue())
        channel = self.bot.get_channel(self.channel_id)
        if channel: await channel.send(f"{user.mention}", embed=embed)

    async def _save_chat_log_to_obsidian(self, user, history, review):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return

        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        filename = f"{now.strftime('%Y%m%d%H%M%S')}-Chat_{user.display_name}.md"
        
        log_parts = []
        for h in history:
            role = "You" if h['role'] == "user" else "AI"
            text = h['parts'][0]
            log_parts.append(f"- **{role}:** {text}")
        
        content = f"# English Chat Log\n\n[[{date_str}]]\n\n## Review\n{review}\n\n## Transcript\n" + "\n".join(log_parts)
        
        # フォルダ確認・作成
        log_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, ENGLISH_LOG_FOLDER)
        if not log_folder:
            log_folder = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, ENGLISH_LOG_FOLDER)
        
        # ファイル作成
        await loop.run_in_executor(None, self._create_text, service, log_folder, filename, content)

        # Daily Note更新
        daily_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, "DailyNotes")
        if daily_folder:
            d_file = await loop.run_in_executor(None, self._find_file, service, daily_folder, f"{date_str}.md")
            cur = ""
            if d_file:
                try:
                    cur = await loop.run_in_executor(None, self._read_text, service, d_file)
                except: pass
            else:
                cur = f"# Daily Note {date_str}\n" # ファイルがない場合は新規作成用のヘッダ

            new = update_section(cur, f"- [[{ENGLISH_LOG_FOLDER}/{filename}|English Chat]]", "## English Logs")
            
            if d_file:
                await loop.run_in_executor(None, self._update_text, service, d_file, new)
            else:
                # デイリーノート自体がない場合は作成
                await loop.run_in_executor(None, self._create_text, service, daily_folder, f"{date_str}.md", new)

async def setup(bot):
    if int(os.getenv("ENGLISH_LEARNING_CHANNEL_ID", 0)) == 0:
        logging.error("EnglishLearningCog: ENGLISH_LEARNING_CHANNEL_ID not set.")
        return
    await bot.add_cog(EnglishLearningCog(bot))