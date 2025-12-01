import os
import json
import asyncio
import logging
import discord
from discord.ext import commands, tasks
from discord import app_commands
import google.generativeai as genai
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError, AuthError
import re
from datetime import time, datetime
import zoneinfo
import aiohttp
import random

# --- Common function import (Obsidian Utils) ---
try:
    from utils.obsidian_utils import update_section
    logging.info("utils/obsidian_utils.py loaded.")
except ImportError:
    logging.warning("utils/obsidian_utils.py not found.")
    def update_section(current_content: str, link_to_add: str, section_header: str) -> str:
        return f"{current_content.strip()}\n{section_header}\n{link_to_add}"

# --- Constants ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
MORNING_SAKUBUN_TIME = time(hour=8, minute=0, tzinfo=JST)
EVENING_SAKUBUN_TIME = time(hour=21, minute=0, tzinfo=JST)
SAKUBUN_NOTE_PATH = "/Study/瞬間英作文リスト.md"
ENGLISH_LOG_PATH = "/English Learning/Chat Logs" 
SAKUBUN_LOG_PATH = "/Study/Sakubun Log" 
DAILY_NOTE_ENGLISH_LOG_HEADER = "## English Learning Logs" 
DAILY_NOTE_SAKUBUN_LOG_HEADER = "## Sakubun Logs" 


# --- Cog: EnglishLearningCog ---
class EnglishLearningCog(commands.Cog, name="EnglishLearning"):
    """Cog for Sakubun and AI English Chat"""

    # --- __init__ ---
    def __init__(self, bot: commands.Bot, gemini_api_key, dropbox_refresh_token, dropbox_app_key, dropbox_app_secret):
        self.bot = bot
        genai.configure(api_key=gemini_api_key)
        self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
        self.dropbox_refresh_token = dropbox_refresh_token
        self.dropbox_app_key = dropbox_app_key
        self.dropbox_app_secret = dropbox_app_secret
        self.dbx = None
        self.session_dir = "/english_sessions" 
        self.chat_sessions = {}
        self.is_ready = False
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault") 
        self.channel_id = int(os.getenv("ENGLISH_LEARNING_CHANNEL_ID", 0)) 
        self.sakubun_questions = [] 

        if dropbox_refresh_token and dropbox_app_key and dropbox_app_secret:
            try:
                self.dbx = dropbox.Dropbox(
                    app_key=self.dropbox_app_key,
                    app_secret=self.dropbox_app_secret,
                    oauth2_refresh_token=self.dropbox_refresh_token
                )
                self.dbx.users_get_current_account() 
                self.is_ready = True
                logging.info("Dropbox client initialized successfully for EnglishLearningCog.")
            except Exception as e:
                logging.error(f"Failed to initialize Dropbox client for EnglishLearningCog: {e}", exc_info=True)
                self.is_ready = False
        else:
            self.is_ready = False 

        if not gemini_api_key: self.is_ready = False
        if self.channel_id == 0: self.is_ready = False

        if self.is_ready:
            self.session = aiohttp.ClientSession()
        else:
            self.session = None

    def _get_session_path(self, user_id: int) -> str:
        return f"{self.session_dir}/{user_id}.json"

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.is_ready: return
        await self._load_sakubun_questions()
        if not self.morning_sakubun_task.is_running():
             self.morning_sakubun_task.start()
        if not self.evening_sakubun_task.is_running():
             self.evening_sakubun_task.start()

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()
        if hasattr(self, 'morning_sakubun_task'): self.morning_sakubun_task.cancel()
        if hasattr(self, 'evening_sakubun_task'): self.evening_sakubun_task.cancel()

    async def _load_sakubun_questions(self):
        if not self.is_ready or not self.dbx: return 
        try:
            path = f"{self.dropbox_vault_path}{SAKUBUN_NOTE_PATH}"
            metadata, res = await asyncio.to_thread(self.dbx.files_download, path)
            content = res.content.decode('utf-8')
            questions = re.findall(r'^\s*-\s*(.+?)(?:\s*::\s*.*)?$', content, re.MULTILINE)
            if questions:
                self.sakubun_questions = [q.strip() for q in questions if q.strip()]
                logging.info(f"Loaded {len(self.sakubun_questions)} Sakubun questions.")
            else:
                logging.warning(f"No questions found in {SAKUBUN_NOTE_PATH}")
        except Exception as e: logging.error(f"Error loading questions: {e}", exc_info=True)

    @tasks.loop(time=MORNING_SAKUBUN_TIME)
    async def morning_sakubun_task(self):
        channel = self.bot.get_channel(self.channel_id)
        if channel:
            await self._run_sakubun_session(channel, 2, "Morning")

    @tasks.loop(time=EVENING_SAKUBUN_TIME)
    async def evening_sakubun_task(self):
        channel = self.bot.get_channel(self.channel_id)
        if channel:
            await self._run_sakubun_session(channel, 2, "Evening")

    @morning_sakubun_task.before_loop
    @evening_sakubun_task.before_loop
    async def before_sakubun_tasks(self):
        await self.bot.wait_until_ready()

    async def _run_sakubun_session(self, channel: discord.TextChannel, num_questions: int, session_name: str):
        if not self.is_ready or not self.sakubun_questions: return

        questions_to_ask = random.sample(self.sakubun_questions, min(num_questions, len(self.sakubun_questions)))

        embed = discord.Embed(
            title=f"✍️ {session_name} Sakubun Challenge ({len(questions_to_ask)} Qs)",
            description=f"Starting now...",
            color=discord.Color.purple()
        )
        await channel.send(embed=embed)
        await asyncio.sleep(20)

        for i, q_text in enumerate(questions_to_ask):
            q_embed = discord.Embed(
                title=f"Q {i+1} / {len(questions_to_ask)}",
                description=f"**{q_text}**",
                color=discord.Color.blue()
            ).set_footer(text="Reply to this message with your English translation.")
            await channel.send(embed=q_embed)
            if i < len(questions_to_ask) - 1:
                await asyncio.sleep(20)

    @app_commands.command(name="english", description="Start/Resume AI English Chat")
    async def english(self, interaction: discord.Interaction):
        if not self.is_ready:
             await interaction.response.send_message("Feature unavailable.", ephemeral=True); return
        if interaction.channel_id != self.channel_id:
             await interaction.response.send_message(f"Please use <#{self.channel_id}>.", ephemeral=True); return
        if interaction.user.id in self.chat_sessions:
             await interaction.response.send_message("Session already active. Use `/end` to stop.", ephemeral=True); return

        await interaction.response.defer()
        user_id = interaction.user.id
        session = await self._load_session_from_dropbox(user_id)

        system_instruction = """
        You are a friendly English conversation partner.
        1. Keep responses short (1-2 sentences).
        2. Keep the conversation going with questions.
        3. Correct mistakes naturally.
        4. Always speak in English.
        """
        model_with_instruction = genai.GenerativeModel("gemini-2.5-pro", system_instruction=system_instruction)

        chat_session = None
        response_text = ""

        try:
            if session is not None:
                chat_session = model_with_instruction.start_chat(history=session)
                resume_prompt = "Hey there! Let's pick up where we left off. What's up?"
                response = await asyncio.wait_for(chat_session.send_message_async(resume_prompt), timeout=60)
                response_text = response.text if response and hasattr(response, "text") else "Hi again! What's new?"
            else:
                chat_session = model_with_instruction.start_chat(history=[])
                initial_prompt = "Hey! Ready to chat in English? How's your day going?"
                response = await asyncio.wait_for(chat_session.send_message_async(initial_prompt), timeout=60)
                response_text = response.text if response and hasattr(response, "text") else "Hi! Let's chat."

        except Exception as e:
            logging.error(f"Error starting chat: {e}")
            response_text = "Sorry, error starting chat."
            if chat_session is None: chat_session = model_with_instruction.start_chat(history=[])

        if chat_session:
            self.chat_sessions[user_id] = chat_session
        else:
             await interaction.followup.send("Failed to start session.", ephemeral=True); return

        await interaction.followup.send(f"**AI:** {response_text}")

    async def _load_session_from_dropbox(self, user_id: int) -> list | None:
        if not self.dbx: return None
        session_path = self._get_session_path(user_id)
        try:
            metadata, res = await asyncio.to_thread(self.dbx.files_download, session_path)
            loaded_data = json.loads(res.content)
            history = []
            for item in loaded_data:
                role = item.get("role")
                parts_list = item.get("parts", [])
                if role and isinstance(parts_list, list):
                     gemini_parts = [{"text": text} for text in parts_list]
                     history.append({"role": role, "parts": gemini_parts})
            return history
        except Exception: return None

    async def _save_session_to_dropbox(self, user_id: int, history: list):
        if not self.dbx: return
        session_path = self._get_session_path(user_id)
        try:
            serializable_history = []
            for turn in history:
                role = getattr(turn, "role", None)
                parts = getattr(turn, "parts", [])
                if role and parts:
                    part_texts = [getattr(p, "text", str(p)) for p in parts]
                    serializable_history.append({"role": role, "parts": part_texts})

            if not serializable_history: return

            content = json.dumps(serializable_history, ensure_ascii=False, indent=2).encode("utf-8")
            await asyncio.to_thread(
                self.dbx.files_upload, content, session_path, mode=WriteMode("overwrite")
            )
        except Exception as e: logging.error(f"Session save failed: {e}")

    async def _generate_chat_review(self, history: list) -> str:
        log_parts = []
        for t in history:
            role = getattr(t, 'role', 'unknown')
            parts = getattr(t, 'parts', [])
            text_content = " ".join(getattr(p, 'text', '') for p in parts)
            if role in ['user', 'model'] and text_content:
                log_parts.append(f"**{'You' if role == 'user' else 'AI'}:** {text_content}")
        conversation_log = "\n".join(log_parts)
        if not conversation_log: return "Not enough conversation for review."

        prompt = f"""You are a professional English teacher. Analyze the conversation log below and provide a review.
# Instructions
1. **Summary**: Briefly summarize the topic (1-2 sentences).
2. **Key Phrases**: Pick 3-5 important words/phrases used or relevant. **Must list under `### Key Phrases` heading as a bulleted list.**
3. **Corrections**: Point out 1-2 grammatical or natural phrasing improvements.
4. **Feedback**: Overall positive feedback in Markdown.
# Log
{conversation_log}
"""
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            return response.text.strip()
        except Exception as e:
            return f"Review generation failed: {type(e).__name__}"

    async def _save_chat_log_to_obsidian(self, user: discord.User, history: list, review: str):
        if not self.dbx or not self.dropbox_vault_path: return

        now = datetime.now(JST); date_str = now.strftime('%Y-%m-%d'); timestamp = now.strftime('%Y%m%d%H%M%S')
        title = f"English Chat Log {user.display_name} {date_str}"
        safe_title_part = re.sub(r'[\\/*?:"<>|]', '_', f"{user.display_name}_{date_str}")
        filename = f"{timestamp}-EnglishChat_{safe_title_part}.md"

        log_parts = []
        for t in history:
            role = getattr(t, 'role', 'unknown')
            parts = getattr(t, 'parts', [])
            text_content = " ".join(getattr(p, 'text', '') for p in parts)
            if role in ['user', 'model'] and text_content:
                log_parts.append(f"- **{'You' if role == 'user' else 'AI'}:** {text_content}")
        conversation_log = "\n".join(log_parts)

        note_content = (f"# {title}\n\n- Date: {date_str}\n- Participant: {user.display_name}\n\n[[{date_str}]]\n"
                        f"---\n## 💬 Session Review\n{review}\n---\n## 📜 Full Transcript\n{conversation_log}\n")
        note_path = f"{self.dropbox_vault_path}{ENGLISH_LOG_PATH}/{filename}"

        try:
            await asyncio.to_thread(self.dbx.files_upload, note_content.encode('utf-8'), note_path, mode=WriteMode('add'))
            
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"; daily_note_content = ""
            try:
                metadata, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                daily_note_content = res.content.decode('utf-8')
            except ApiError: daily_note_content = "" # ★ 修正: 初期値を空文字に変更

            note_filename_for_link = filename.replace('.md', ''); link_path_part = ENGLISH_LOG_PATH.lstrip('/')
            link_to_add = f"- [[{link_path_part}/{note_filename_for_link}|English Chat ({user.display_name})]]"

            new_daily_content = update_section(daily_note_content, link_to_add, DAILY_NOTE_ENGLISH_LOG_HEADER)
            await asyncio.to_thread(self.dbx.files_upload, new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))

        except Exception as e: logging.error(f"Chat log save error: {e}")

    async def _save_sakubun_log_to_obsidian(self, japanese_question: str, user_answer: str, feedback_text: str):
        if not self.dbx or not self.dropbox_vault_path: return

        now = datetime.now(JST); date_str = now.strftime('%Y-%m-%d'); timestamp = now.strftime('%Y%m%d%H%M%S')
        safe_title_part = re.sub(r'[\\/*?:"<>|]', '_', japanese_question[:20]); filename = f"{timestamp}-Sakubun_{safe_title_part}.md"

        model_answers_match = re.search(r"^\#+\s*Model Answer(?:s)?\s*?\n+((?:^\s*[-*+].*(?:\n|$))+)", feedback_text, re.DOTALL | re.MULTILINE | re.IGNORECASE)
        model_answers = ""
        if model_answers_match:
            raw_answers = re.findall(r"^\s*[-*+]\s+(.+)", model_answers_match.group(1), re.MULTILINE)
            model_answers = "\n".join([f"- {ans.strip()}" for ans in raw_answers if ans.strip()])

        note_content = (f"# {date_str} Sakubun\n- Date: [[{date_str}]]\n---\n## Question\n{japanese_question}\n"
                        f"## Your Answer\n{user_answer}\n## AI Feedback\n{feedback_text}\n")
        if model_answers: note_content += f"---\n## Model Answer\n{model_answers}\n"
        note_path = f"{self.dropbox_vault_path}{SAKUBUN_LOG_PATH}/{filename}"

        try:
            await asyncio.to_thread(self.dbx.files_upload, note_content.encode('utf-8'), note_path, mode=WriteMode('add'))

            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"; daily_note_content = ""
            try:
                metadata, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                daily_note_content = res.content.decode('utf-8')
            except ApiError: daily_note_content = "" # ★ 修正: 初期値を空文字に変更

            note_filename_for_link = filename.replace('.md', ''); link_path_part = SAKUBUN_LOG_PATH.lstrip('/')
            link_to_add = f"- [[{link_path_part}/{note_filename_for_link}|{japanese_question[:30]}...]]"
            new_daily_content = update_section(daily_note_content, link_to_add, DAILY_NOTE_SAKUBUN_LOG_HEADER)
            await asyncio.to_thread(self.dbx.files_upload, new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))

        except Exception as e: logging.error(f"Sakubun log save error: {e}")

    @app_commands.command(name="end", description="End English chat")
    async def end_chat(self, interaction: discord.Interaction):
        if not self.is_ready: return
        if interaction.channel_id != self.channel_id: return

        user_id = interaction.user.id
        session_path = self._get_session_path(user_id)
        chat_session = self.chat_sessions.pop(user_id, None)

        if not chat_session:
             await interaction.response.send_message("No active session.", ephemeral=True); return

        await interaction.response.defer()

        review_text = "Failed to generate review."
        history_to_save = []

        if hasattr(chat_session, 'history'):
            history_to_save = chat_session.history
            try:
                review_text = await self._generate_chat_review(history_to_save)
                if self.dbx:
                    await self._save_chat_log_to_obsidian(interaction.user, history_to_save, review_text)
            except Exception as e: logging.error(f"End chat error: {e}")

        review_embed = discord.Embed(
            title="💬 Conversation Review",
            description=review_text[:4000],
            color=discord.Color.gold(),
            timestamp=datetime.now(JST)
        ).set_footer(text=f"{interaction.user.display_name}'s session")

        await interaction.followup.send(embed=review_embed)

        if self.dbx:
            try:
                await asyncio.to_thread(self.dbx.files_delete_v2, session_path)
            except Exception: pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if (not self.is_ready or message.author.bot or message.channel.id != self.channel_id or message.content.startswith('/')): return

        user_id = message.author.id

        if message.reference and message.reference.message_id:
            try:
                original_msg = await message.channel.fetch_message(message.reference.message_id)
                if (original_msg.author.id == self.bot.user.id and original_msg.embeds and
                        "Q" in original_msg.embeds[0].title):
                    await self.handle_sakubun_answer(message, message.content.strip(), original_msg)
                    return
            except: pass

        if user_id in self.chat_sessions:
            chat = self.chat_sessions[user_id]
            async with message.channel.typing():
                try:
                    response = await chat.send_message_async(message.content)
                    response_text = response.text if response and hasattr(response, 'text') else "Error."
                    await message.reply(f"**AI:** {response_text}")
                    await self._save_session_to_dropbox(user_id, chat.history)
                except Exception:
                    await message.reply("Error processing message.")

    async def handle_sakubun_answer(self, message: discord.Message, user_answer: str, original_msg: discord.Message):
        if not self.is_ready: return
        if not user_answer: return

        await message.add_reaction("🤔")
        japanese_question = original_msg.embeds[0].description.strip().replace("*","")

        # 修正箇所: プロンプトを日本語で解説するように変更
        prompt = f"""あなたはプロの英語教師です。生徒の翻訳（英作文）を添削してください。
# 指示
1. **評価**: 良かった点と改善点を日本語で指摘してください。
2. **添削**: より自然で文法的に正しい表現を提案してください。
3. **重要フレーズ**: 今回の表現で使える重要な語句を3〜5個選んでください。**必ず `### Key Phrases` という見出しの下に箇条書きで列挙してください。**
4. **模範解答**: 2〜3パターンの模範解答を `### Model Answer` という見出しの下に提示してください。
5. **文法ポイント**: 文法の解説を日本語で簡潔に行ってください。
6. **フォーマット**: Markdown形式で見やすく整形してください。解説はすべて日本語で行ってください。

# 元の文章（日本語）
{japanese_question}
# 生徒の回答（英語）
{user_answer}"""

        feedback_text = "Feedback generation failed."
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            if response and hasattr(response, 'text'): feedback_text = response.text

            feedback_embed = discord.Embed(title=f"Feedback: {japanese_question}", description=feedback_text[:4000], color=discord.Color.green())
            await message.reply(embed=feedback_embed)
            await self._save_sakubun_log_to_obsidian(japanese_question, user_answer, feedback_text)

        except Exception: await message.reply("Error generating feedback.")
        finally:
             try: await message.remove_reaction("🤔", self.bot.user)
             except: pass

async def setup(bot):
    gemini_key = os.getenv("GEMINI_API_KEY")
    dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
    dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
    dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
    channel_id = os.getenv("ENGLISH_LEARNING_CHANNEL_ID")

    if not all([gemini_key, dropbox_refresh_token, dropbox_app_key, dropbox_app_secret, channel_id]): return

    cog_instance = EnglishLearningCog(bot, gemini_key, dropbox_refresh_token, dropbox_app_key, dropbox_app_secret)
    if cog_instance.is_ready:
        await bot.add_cog(cog_instance)