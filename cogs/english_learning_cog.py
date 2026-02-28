import os
import asyncio
import logging
import discord
from discord.ext import commands, tasks
from discord import app_commands
import random
from datetime import time, datetime

from google.genai import types

# --- リファクタリング: インポート整理と定数化 ---
from config import JST
from utils.obsidian_utils import update_section

MORNING_SAKUBUN_TIME = time(hour=8, minute=0, tzinfo=JST)
EVENING_SAKUBUN_TIME = time(hour=21, minute=0, tzinfo=JST)
SAKUBUN_FILE_NAME = "瞬間英作文リスト.md"
ENGLISH_LOG_FOLDER = "English Learning"

class EnglishLearningCog(commands.Cog, name="EnglishLearning"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = int(os.getenv("ENGLISH_LEARNING_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        
        # --- リファクタリング: Bot本体のサービスを使い回す ---
        self.drive_service = bot.drive_service
        self.gemini_client = bot.gemini_client
        
        self.chat_sessions = {} # {user_id: [Content objects...]}
        self.sakubun_questions = []
        self.is_ready = bool(self.drive_folder_id and self.channel_id)

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready:
            await self._load_sakubun_questions()
            self.morning_sakubun_task.start()
            self.evening_sakubun_task.start()

    async def _load_sakubun_questions(self):
        service = self.drive_service.get_service()
        if not service: return
        
        study_folder = await self.drive_service.find_file(service, self.drive_folder_id, "Study")
        target_folder = study_folder if study_folder else self.drive_folder_id
        
        file_id = await self.drive_service.find_file(service, target_folder, SAKUBUN_FILE_NAME)
        if file_id:
            try:
                content = await self.drive_service.read_text_file(service, file_id)
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
        self.chat_sessions[user_id] = [] # 履歴初期化 (list of Content dicts)
        
        await interaction.response.send_message("Let's start English conversation! 🇺🇸\n(Type `end` or `finish` to stop and save the log.)")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.channel.id != self.channel_id: return
        
        user_id = message.author.id
        if user_id in self.chat_sessions:
            content = message.content.strip()
            
            if content.lower() in ["end", "finish", "quit", "終了"]:
                await message.channel.send("Conversation ended. Saving log...")
                await self._finish_session(message.author)
                return

            self.chat_sessions[user_id].append(
                types.Content(role="user", parts=[types.Part.from_text(text=content)])
            )
            
            async with message.channel.typing():
                try:
                    if self.gemini_client:
                        response = await self.gemini_client.aio.models.generate_content(
                            model='gemini-2.0-flash',
                            contents=self.chat_sessions[user_id]
                        )
                        ai_text = response.text
                        
                        self.chat_sessions[user_id].append(
                            types.Content(role="model", parts=[types.Part.from_text(text=ai_text)])
                        )
                        await message.channel.send(ai_text)
                    else:
                        await message.channel.send("AI Client not initialized.")
                except Exception as e:
                    await message.channel.send(f"⚠️ Error: {e}")

    async def _finish_session(self, user):
        user_id = user.id
        history = self.chat_sessions.get(user_id, [])
        if not history:
            del self.chat_sessions[user_id]
            return

        full_text = ""
        for h in history:
            role = h.role
            text = h.parts[0].text if h.parts else ""
            full_text += f"{role}: {text}\n"

        prompt = f"""
        以下の英会話ログを分析し、以下の項目を出力してください。
        1. **Summary**: 会話の内容の要約 (日本語)
        2. **Corrections**: ユーザーの英語の誤りと、より自然な表現の提案 (箇条書き)
        3. **Advice**: 今後の学習アドバイス

        --- Log ---
        {full_text}
        """
        try:
            if self.gemini_client:
                res = await self.gemini_client.aio.models.generate_content(
                    model='gemini-2.5-pro',
                    contents=prompt
                )
                review = res.text
            else:
                review = "AI Error"
        except: 
            review = "Review generation failed."

        await self._save_chat_log_to_obsidian(user, history, review)
        
        del self.chat_sessions[user_id]
        
        embed = discord.Embed(title="📝 Conversation Review", description=review[:4000], color=discord.Color.blue())
        channel = self.bot.get_channel(self.channel_id)
        if channel: await channel.send(f"{user.mention}", embed=embed)

    async def _save_chat_log_to_obsidian(self, user, history, review):
        service = self.drive_service.get_service()
        if not service: return

        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        filename = f"{now.strftime('%Y%m%d%H%M%S')}-Chat_{user.display_name}.md"
        
        log_parts = []
        for h in history:
            role = "You" if h.role == "user" else "AI"
            text = h.parts[0].text if h.parts else ""
            log_parts.append(f"- **{role}:** {text}")
        
        content = f"# English Chat Log\n\n[[{date_str}]]\n\n## Review\n{review}\n\n## Transcript\n" + "\n".join(log_parts)
        
        log_folder = await self.drive_service.find_file(service, self.drive_folder_id, ENGLISH_LOG_FOLDER)
        if not log_folder:
            log_folder = await self.drive_service.create_folder(service, self.drive_folder_id, ENGLISH_LOG_FOLDER)
        
        await self.drive_service.upload_text(service, log_folder, filename, content)

        daily_folder = await self.drive_service.find_file(service, self.drive_folder_id, "DailyNotes")
        if daily_folder:
            d_file = await self.drive_service.find_file(service, daily_folder, f"{date_str}.md")
            cur = ""
            if d_file:
                cur = await self.drive_service.read_text_file(service, d_file)
            else:
                cur = f"# Daily Note {date_str}\n"

            new = update_section(cur, f"- [[{ENGLISH_LOG_FOLDER}/{filename}|English Chat]]", "## English Logs")
            
            if d_file:
                await self.drive_service.update_text(service, d_file, new)
            else:
                await self.drive_service.upload_text(service, daily_folder, f"{date_str}.md", new)

async def setup(bot):
    if int(os.getenv("ENGLISH_LEARNING_CHANNEL_ID", 0)) == 0:
        logging.error("EnglishLearningCog: ENGLISH_LEARNING_CHANNEL_ID not set.")
        return
    await bot.add_cog(EnglishLearningCog(bot))