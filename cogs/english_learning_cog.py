import os
import json
import asyncio
import logging
import discord
from discord.ext import commands, tasks
from discord import app_commands
from openai import AsyncOpenAI
import google.generativeai as genai
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError, AuthError
import io
import re
from datetime import time, datetime
import zoneinfo
import aiohttp
import random

# --- Google Docs Handler Import ---
try:
    from google_docs_handler import append_text_to_doc_async
    google_docs_enabled = True
    logging.info("Google Docs連携が有効です。")
except ImportError:
    logging.warning("google_docs_handler.pyが見つからないため、Google Docs連携は無効です。")
    google_docs_enabled = False
    # Define a dummy async function if import fails
    async def append_text_to_doc_async(*args, **kwargs):
        logging.warning("Google Docs handler is not available.")
        pass # Do nothing


# --- Common function import (Obsidian Utils) ---
try:
    from utils.obsidian_utils import update_section
    logging.info("utils/obsidian_utils.pyを読み込みました。")
except ImportError:
    logging.warning("utils/obsidian_utils.pyが見つかりません。")
    # Define a dummy function if import fails
    def update_section(current_content: str, link_to_add: str, section_header: str) -> str:
        if section_header in current_content:
            lines = current_content.split('\n')
            try:
                # Find the line index containing the section header (case-insensitive)
                header_index = -1
                for i, line in enumerate(lines):
                    if line.strip().lower() == section_header.lower():
                        header_index = i
                        break
                if header_index == -1: raise ValueError("Header not found")

                insert_index = header_index + 1
                # Find the next header or end of file
                while insert_index < len(lines) and not lines[insert_index].strip().startswith('## '):
                    insert_index += 1

                # Ensure blank line before adding if needed
                if insert_index > header_index + 1 and lines[insert_index - 1].strip() != "":
                    lines.insert(insert_index, "")
                    insert_index += 1 # Adjust index after insertion

                # Insert the new link/text
                lines.insert(insert_index, link_to_add)
                return "\n".join(lines)
            except ValueError:
                # Append if header exists but something went wrong finding insertion point
                 logging.warning(f"Could not find insertion point for {section_header}, appending.")
                 return f"{current_content.strip()}\n\n{section_header}\n{link_to_add}\n"

        else:
            # Append new section at the end if header doesn't exist
            # (Ideally, use SECTION_ORDER logic here if available)
            logging.info(f"Section '{section_header}' not found, appending to the end.")
            return f"{current_content.strip()}\n\n{section_header}\n{link_to_add}\n"

# --- Constants ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
MORNING_SAKUBUN_TIME = time(hour=8, minute=0, tzinfo=JST)
EVENING_SAKUBUN_TIME = time(hour=21, minute=0, tzinfo=JST)
SAKUBUN_NOTE_PATH = "/Study/瞬間英作文リスト.md"
ENGLISH_LOG_PATH = "/English Learning/Chat Logs" # 英会話ログ保存先 (Obsidian Vault内)
SAKUBUN_LOG_PATH = "/Study/Sakubun Log" # 瞬間英作文ログ保存先
DAILY_NOTE_ENGLISH_LOG_HEADER = "## English Learning Logs" # デイリーノートの見出し名 (英会話)
DAILY_NOTE_SAKUBUN_LOG_HEADER = "## Sakubun Logs" # デイリーノートの見出し名 (瞬間英作文)


# --- Helper Function to Extract Phrases ---
def extract_phrases_from_markdown_list(text: str, heading: str) -> list[str]:
    """特定のMarkdown見出しの下にある箇条書き項目を抽出する"""
    phrases = []
    try:
        # 見出しの下のセクションを見つける正規表現 (ヘッダーレベル不問、大文字小文字無視)
        # Ensure it captures list items even if indented or having extra spaces
        # --- Updated regex to handle optional spaces after heading ---
        pattern = rf"^\#+\s*{re.escape(heading)}\s*?\n((?:^\s*[-*+]\s+.*(?:\n|$))+)"
        match = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)

        if match:
            list_section = match.group(1)
            # 個々のリスト項目（箇条書き記号の後のテキスト）を抽出
            raw_phrases = re.findall(r"^\s*[-*+]\s+(.+)", list_section, re.MULTILINE)
            # フレーズ内のMarkdown記号を除去し、前後の空白を削除
            phrases = [re.sub(r'[*_`~]', '', p.strip()) for p in raw_phrases if p.strip()] # 空の項目は除外
            logging.info(f"見出し '{heading}' の下からフレーズを抽出しました: {phrases}")
        else:
            logging.warning(f"指定された見出し '{heading}' またはその下の箇条書きが見つかりませんでした。")
    except Exception as e:
        logging.error(f"見出し '{heading}' の下のフレーズ抽出中にエラー: {e}", exc_info=True)
    return phrases


# --- UI Component: TTSView ---
class TTSView(discord.ui.View):
    MAX_BUTTONS = 5 # Limit to 5 buttons per view for clarity

    def __init__(self, phrases_or_text: list[str] | str, openai_client):
        super().__init__(timeout=3600) # Keep timeout 1 hour
        self.openai_client = openai_client
        self.phrases = []

        if isinstance(phrases_or_text, str):
            # Clean mentions and markdown from single text input
            clean_text = re.sub(r'<@!?\d+>', '', phrases_or_text) # Remove mentions
            clean_text = re.sub(r'[*_`~#]', '', clean_text) # Remove markdown
            full_text = clean_text.strip()[:2000] # Limit length for API safety
            if full_text:
                self.phrases.append(full_text)
                # Truncate label text for button display
                label = (full_text[:25] + '...') if len(full_text) > 28 else full_text
                button = discord.ui.Button(
                    label=f"🔊 {label}", style=discord.ButtonStyle.secondary, custom_id="tts_phrase_0"
                )
                button.callback = self.tts_button_callback
                self.add_item(button)
        elif isinstance(phrases_or_text, list):
            self.phrases = phrases_or_text[:self.MAX_BUTTONS] # Limit number of phrases
            for index, phrase in enumerate(self.phrases):
                # Clean markdown from each phrase in the list
                clean_phrase = re.sub(r'[*_`~#]', '', phrase.strip())[:2000] # Limit length
                if not clean_phrase: continue # Skip empty phrases after cleaning
                # Truncate label text
                label = (clean_phrase[:25] + '...') if len(clean_phrase) > 28 else clean_phrase
                button = discord.ui.Button(
                    label=f"🔊 {label}", style=discord.ButtonStyle.secondary,
                    custom_id=f"tts_phrase_{index}", row=index // 5 # Basic row management (though MAX_BUTTONS limits this)
                )
                button.callback = self.tts_button_callback
                self.add_item(button)

    async def tts_button_callback(self, interaction: discord.Interaction):
        custom_id = interaction.data.get("custom_id")
        logging.info(f"TTSボタンクリック: {custom_id} by {interaction.user}")

        # Helper function for sending messages (handles deferral)
        async def send_response(msg: str, **kwargs):
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True, **kwargs)
            else:
                await interaction.response.send_message(msg, ephemeral=True, **kwargs)

        if not custom_id or not custom_id.startswith("tts_phrase_"):
            await send_response("無効なボタンIDです。", delete_after=10)
            return
        try:
            phrase_index = int(custom_id.split("_")[-1])
            if not (0 <= phrase_index < len(self.phrases)):
                 await send_response("無効なフレーズインデックスです。", delete_after=10)
                 return

            phrase_to_speak = self.phrases[phrase_index]
            if not phrase_to_speak:
                 await send_response("空のフレーズは読み上げできません。", delete_after=10)
                 return
            if not self.openai_client:
                 await send_response("TTS機能が設定されていません (OpenAI APIキー未設定)。", delete_after=10)
                 return

            # Defer only if not already done
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=True)

            # Generate speech using OpenAI API
            # --- Updated API call for newer openai versions ---
            response = await self.openai_client.audio.speech.create(
                model="tts-1", voice="alloy", input=phrase_to_speak, response_format="mp3"
            )
            audio_bytes = response.content # Access content directly
            # --- End of update ---

            audio_buffer = io.BytesIO(audio_bytes)
            audio_file = discord.File(fp=audio_buffer, filename=f"phrase_{phrase_index}.mp3")
            # Always use followup after deferring
            await interaction.followup.send(f"🔊 \"{phrase_to_speak}\"", file=audio_file, ephemeral=True)
        except ValueError:
            logging.error(f"custom_idからインデックスの解析に失敗: {custom_id}")
            # Always use followup after deferring
            await interaction.followup.send("ボタン処理エラー。", ephemeral=True)
        except openai.APIError as e: # Catch specific OpenAI errors
             logging.error(f"OpenAI APIエラー (TTS生成中): {e}", exc_info=True)
             await interaction.followup.send(f"音声生成中にOpenAI APIエラーが発生しました: {e}", ephemeral=True)
        except Exception as e:
            logging.error(f"tts_button_callback内でエラー: {e}", exc_info=True)
            # Always use followup after deferring
            await interaction.followup.send(f"音声の生成・送信中にエラーが発生しました: {e}", ephemeral=True)


# --- Cog: EnglishLearningCog ---
class EnglishLearningCog(commands.Cog, name="EnglishLearning"):
    """瞬間英作文とAI壁打ちチャットによる英語学習を支援するCog"""

    # --- __init__ ---
    def __init__(self, bot: commands.Bot, openai_api_key, gemini_api_key, dropbox_refresh_token, dropbox_app_key, dropbox_app_secret):
        self.bot = bot
        self.openai_client = AsyncOpenAI(api_key=openai_api_key) if openai_api_key else None
        genai.configure(api_key=gemini_api_key)
        self.gemini_model = genai.GenerativeModel("gemini-2.5-pro") # Use pro model
        self.dropbox_refresh_token = dropbox_refresh_token
        self.dropbox_app_key = dropbox_app_key
        self.dropbox_app_secret = dropbox_app_secret
        self.dbx = None
        self.session_dir = "/english_sessions" # Dropbox内のパス (ルートからの想定)
        self.chat_sessions = {}
        self.is_ready = False
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault") # Default vault path
        self.channel_id = int(os.getenv("ENGLISH_LEARNING_CHANNEL_ID", 0)) # Channel ID for commands/messages
        self.sakubun_questions = [] # Cache for Sakubun questions

        # Initialize Dropbox client
        if dropbox_refresh_token and dropbox_app_key and dropbox_app_secret:
            try:
                self.dbx = dropbox.Dropbox(
                    app_key=self.dropbox_app_key,
                    app_secret=self.dropbox_app_secret,
                    oauth2_refresh_token=self.dropbox_refresh_token
                )
                self.dbx.users_get_current_account() # Test connection
                self.is_ready = True # Initial readiness based on Dropbox
                logging.info("Dropbox client initialized successfully for EnglishLearningCog.")
            except AuthError as e:
                logging.error(f"Dropbox AuthError during initialization for EnglishLearningCog: {e}. Cog will be partially functional.")
                # Allow partial readiness if only Dropbox fails? Let's keep is_ready=False for now.
                self.is_ready = False
            except Exception as e:
                logging.error(f"Failed to initialize Dropbox client for EnglishLearningCog: {e}", exc_info=True)
                self.is_ready = False
        else:
            logging.warning("Dropbox credentials missing. Session saving/loading will be disabled.")
            self.is_ready = False # Dropbox is required for persistence

        # Check other requirements and update readiness
        if not self.openai_client: logging.warning("OpenAI API Key not found. TTS functionality will be disabled.")
        # Don't overwrite is_ready if Dropbox failed
        if not gemini_api_key: logging.error("Gemini API key missing. Cog cannot function."); self.is_ready = False
        if self.channel_id == 0: logging.error("ENGLISH_LEARNING_CHANNEL_ID is not set. Cog cannot function."); self.is_ready = False

        # Initialize aiohttp session only if ready
        if self.is_ready:
            self.session = aiohttp.ClientSession()
        else:
            self.session = None # Ensure session is None if not ready

        logging.info(f"EnglishLearning Cog initialization finished. Ready: {self.is_ready}")

    # --- _get_session_path ---
    def _get_session_path(self, user_id: int) -> str:
        # vault_path を考慮しない（session_dir がルートからのパス）
        return f"{self.session_dir}/{user_id}.json"

    # --- on_ready ---
    @commands.Cog.listener()
    async def on_ready(self):
        if not self.is_ready: return
        # Load questions when ready
        await self._load_sakubun_questions()
        # Start tasks if not already running
        if not self.morning_sakubun_task.is_running():
             self.morning_sakubun_task.start()
             logging.info("Morning Sakubun task started.")
        if not self.evening_sakubun_task.is_running():
             self.evening_sakubun_task.start()
             logging.info("Evening Sakubun task started.")
        logging.info("EnglishLearningCog is ready and tasks are scheduled.")


    # --- cog_unload ---
    async def cog_unload(self):
        # Close session only if it was initialized
        if self.session and not self.session.closed:
            await self.session.close()
        # Cancel tasks only if they might be running
        if hasattr(self, 'morning_sakubun_task'): self.morning_sakubun_task.cancel()
        if hasattr(self, 'evening_sakubun_task'): self.evening_sakubun_task.cancel()
        logging.info("EnglishLearningCog unloaded.")

    # --- _load_sakubun_questions ---
    async def _load_sakubun_questions(self):
        if not self.is_ready or not self.dbx: return # Check Dropbox client
        try:
            # Construct full path within vault
            path = f"{self.dropbox_vault_path}{SAKUBUN_NOTE_PATH}"
            logging.info(f"Loading Sakubun questions from: {path}")
            # Use asyncio.to_thread for Dropbox calls
            metadata, res = await asyncio.to_thread(self.dbx.files_download, path)
            content = res.content.decode('utf-8')
            # Regex to find list items, potentially ignoring model answers ("::")
            questions = re.findall(r'^\s*-\s*(.+?)(?:\s*::\s*.*)?$', content, re.MULTILINE)
            if questions:
                self.sakubun_questions = [q.strip() for q in questions if q.strip()] # Filter empty questions
                logging.info(f"Obsidianから{len(self.sakubun_questions)}問の瞬間英作文の問題を読み込みました。")
            else:
                logging.warning(f"Obsidianのファイル ({SAKUBUN_NOTE_PATH}) に問題が見つかりませんでした (形式: '- 日本語文')。")
        except AuthError as e: logging.error(f"Dropbox AuthError loading Sakubun questions: {e}")
        except ApiError as e:
            # Handle specific "not found" error
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                logging.warning(f"瞬間英作文ファイルが見つかりません: {path}")
            else: logging.error(f"Dropbox APIエラー (瞬間英作文読み込み): {e}")
        except Exception as e: logging.error(f"Obsidianからの問題読み込み中に予期せぬエラー: {e}", exc_info=True)

    # --- morning_sakubun_task, evening_sakubun_task ---
    @tasks.loop(time=MORNING_SAKUBUN_TIME)
    async def morning_sakubun_task(self):
        channel = self.bot.get_channel(self.channel_id)
        if channel:
            # --- Ask 2 questions ---
            await self._run_sakubun_session(channel, 2, "朝")
            # --- End modification ---
        else: logging.error(f"Sakubun channel not found: {self.channel_id}")

    @tasks.loop(time=EVENING_SAKUBUN_TIME)
    async def evening_sakubun_task(self):
        channel = self.bot.get_channel(self.channel_id)
        if channel:
            # --- Ask 2 questions ---
            await self._run_sakubun_session(channel, 2, "夜")
            # --- End modification ---
        else: logging.error(f"Sakubun channel not found: {self.channel_id}")

    # ループ開始前にBotの準備を待つ
    @morning_sakubun_task.before_loop
    @evening_sakubun_task.before_loop
    async def before_sakubun_tasks(self):
        await self.bot.wait_until_ready()
        logging.info("Sakubun tasks waiting for bot readiness...")

    # --- _run_sakubun_session ---
    async def _run_sakubun_session(self, channel: discord.TextChannel, num_questions: int, session_name: str):
        if not self.is_ready: return
        if not self.sakubun_questions:
            await channel.send("⚠️ 瞬間英作文の問題リストが空のため、出題できません。Obsidianのファイルを確認してください。"); return

        # Sample questions
        questions_to_ask = random.sample(self.sakubun_questions, min(num_questions, len(self.sakubun_questions)))

        # Send introductory embed
        embed = discord.Embed(
            title=f"✍️ 今日の{session_name}・瞬間英作文 ({len(questions_to_ask)}問)",
            description=f"これから{len(questions_to_ask)}問出題します。",
            color=discord.Color.purple()
        ).set_footer(text="約20秒後に最初の問題が出題されます。")
        await channel.send(embed=embed)
        await asyncio.sleep(20) # Wait before first question

        # Ask each question with a delay
        for i, q_text in enumerate(questions_to_ask):
            q_embed = discord.Embed(
                title=f"第 {i+1} 問 / {len(questions_to_ask)} 問",
                description=f"**{q_text}**", # Question text in bold
                color=discord.Color.blue()
            ).set_footer(text="このメッセージに返信する形で英訳を投稿してください。")
            await channel.send(embed=q_embed)
            # Wait before the next question (if any)
            if i < len(questions_to_ask) - 1:
                await asyncio.sleep(20) # Wait 20 seconds between questions


    # --- /english command ---
    @app_commands.command(name="english", description="AIとの英会話チャットを開始または再開します。")
    async def english(self, interaction: discord.Interaction):
        if not self.is_ready:
             await interaction.response.send_message("英会話機能は現在利用できません（設定確認中）。", ephemeral=True); return
        # Check if command is used in the correct channel
        if interaction.channel_id != self.channel_id:
             await interaction.response.send_message(f"このコマンドは英会話チャンネル (<#{self.channel_id}>) でのみ利用できます。", ephemeral=True); return
        # Check if user already has a session
        if interaction.user.id in self.chat_sessions:
             await interaction.response.send_message("既にセッションを開始しています。終了は `/end`。", ephemeral=True); return

        await interaction.response.defer() # Defer response as loading/starting can take time
        user_id = interaction.user.id
        session_path = self._get_session_path(user_id)
        session = await self._load_session_from_dropbox(user_id) # Returns None if dbx unavailable or error

        # Define system instruction for the AI model
        system_instruction = "あなたはフレンドリーな英会話の相手です。ユーザーのメッセージに共感したり、質問を返したりして、会話を弾ませてください。もしユーザーの英語に文法的な誤りや不自然な点があれば、会話の流れを止めないように優しく指摘し、正しい表現を提案してください。例：「`I go to the park yesterday.` → `Oh, you went to the park yesterday! What did you do there?`」のように、自然な訂正を会話に含めてください。あなたの返答は、常に自然な英語で行ってください。"
        model_with_instruction = genai.GenerativeModel("gemini-2.5-pro", system_instruction=system_instruction)

        chat_session = None
        response_text = ""

        try:
            # Resume session if history exists
            if session is not None:
                logging.info(f"セッション再開: {session_path}")
                chat_session = model_with_instruction.start_chat(history=session)
                # Send a resume message
                response = await asyncio.wait_for(chat_session.send_message_async("Welcome back! Let's continue our English conversation. How have you been?"), timeout=60)
                response_text = response.text if response and hasattr(response, "text") else "Hi again! Let's chat."
            # Start new session if no history
            else:
                logging.info(f"新規セッション開始: {session_path}")
                chat_session = model_with_instruction.start_chat(history=[])
                # Send an initial greeting
                initial_prompt = "Hi! I'm your AI English partner. Let's chat! How's it going?"
                response = await asyncio.wait_for(chat_session.send_message_async(initial_prompt), timeout=60)
                response_text = response.text if response and hasattr(response, "text") else "Hi! Let's chat."

        except asyncio.TimeoutError:
            logging.error(f"Chat start/resume timeout for user {user_id}")
            response_text = "Sorry, the response timed out. Let's try starting. How are you?"
            # Ensure session is created even on timeout
            if chat_session is None: chat_session = model_with_instruction.start_chat(history=[])
        except Exception as e:
            logging.error(f"Error starting/resuming chat session for {user_id}: {e}", exc_info=True)
            response_text = "Sorry, an error occurred while starting our chat. Let's try simply. How are you?"
            # Ensure session is created even on error
            if chat_session is None: chat_session = model_with_instruction.start_chat(history=[])

        # Store the session if created
        if chat_session:
            self.chat_sessions[user_id] = chat_session
        else:
             # If session creation failed critically
             await interaction.followup.send("チャットセッションを開始できませんでした。", ephemeral=True); return

        # Send the first AI message with TTS button if available
        view = TTSView(response_text, self.openai_client) if self.openai_client else None
        await interaction.followup.send(f"**AI:** {response_text}", view=view)

        # Send ephemeral hint to user
        try:
            await interaction.followup.send("会話を続けるには、メッセージを送信してください。終了は `/end`", ephemeral=True)
        except Exception as e:
             # Log error if sending the ephemeral hint fails (rare)
             logging.error(f"Unexpected error sending ephemeral followup: {e}", exc_info=True)

    # --- _load_session_from_dropbox ---
    async def _load_session_from_dropbox(self, user_id: int) -> list | None:
        if not self.dbx: return None # Return None if Dropbox client isn't ready
        session_path = self._get_session_path(user_id)
        try:
            logging.info(f"Loading session from: {session_path}")
            # Use asyncio.to_thread for Dropbox call
            metadata, res = await asyncio.to_thread(self.dbx.files_download, session_path)
            loaded_data = json.loads(res.content)
            # Convert loaded JSON data back to Gemini history format
            history = []
            for item in loaded_data:
                role = item.get("role")
                parts_list = item.get("parts", [])
                # Basic validation
                if role and isinstance(parts_list, list) and all(isinstance(p, str) for p in parts_list):
                     # Gemini parts format is a list of dicts with 'text' key
                     gemini_parts = [{"text": text} for text in parts_list]
                     history.append({"role": role, "parts": gemini_parts})
                else:
                     # Log and skip invalid items
                     logging.warning(f"Skipping invalid history item for user {user_id}: {item}")
            logging.info(f"Successfully loaded and formatted session for user {user_id}")
            return history
        except AuthError as e: logging.error(f"Dropbox AuthError loading session ({session_path}): {e}. Check token validity."); return None
        except ApiError as e:
            # Handle specific "not found" error
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found(): logging.info(f"Session file not found for {user_id} at {session_path}"); return None
            # Log other API errors
            logging.error(f"Dropbox APIエラー ({session_path}): {e}"); return None
        except json.JSONDecodeError as json_e: logging.error(f"JSON解析失敗 ({session_path}): {json_e}"); return None
        except Exception as e: logging.error(f"セッション読込エラー ({session_path}): {e}", exc_info=True); return None

    # --- _save_session_to_dropbox ---
    async def _save_session_to_dropbox(self, user_id: int, history: list):
        if not self.dbx: return # Skip if Dropbox client isn't ready
        session_path = self._get_session_path(user_id)
        try:
            # Convert Gemini history object to serializable list of dicts
            serializable_history = []
            for turn in history:
                # Access attributes safely
                role = getattr(turn, "role", None)
                parts = getattr(turn, "parts", [])
                if role and parts:
                    # Extract text content from each part
                    part_texts = [getattr(p, "text", str(p)) for p in parts] # Fallback to str()
                    serializable_history.append({"role": role, "parts": part_texts})

            # Check if there's anything to save
            if not serializable_history: logging.warning(f"History for user {user_id} is empty or not serializable. Skipping save."); return

            # Convert to JSON bytes
            content = json.dumps(serializable_history, ensure_ascii=False, indent=2).encode("utf-8")
            # Use asyncio.to_thread for Dropbox call
            await asyncio.to_thread(
                self.dbx.files_upload, content, session_path, mode=WriteMode("overwrite")
            )
            logging.info(f"Saved session to: {session_path}")
        except AuthError as e: logging.error(f"Dropbox AuthError saving session ({session_path}): {e}. Check token validity.")
        except Exception as e: logging.error(f"セッション保存失敗 ({session_path}): {e}", exc_info=True)

    # --- _generate_chat_review ---
    async def _generate_chat_review(self, history: list) -> str:
        # Format conversation log for the prompt
        log_parts = []
        for t in history:
            role = getattr(t, 'role', 'unknown')
            parts = getattr(t, 'parts', [])
            # Combine text from all parts in a turn
            text_content = " ".join(getattr(p, 'text', '') for p in parts)
            # Include only user and model turns with content
            if role in ['user', 'model'] and text_content:
                log_parts.append(f"**{'You' if role == 'user' else 'AI'}:** {text_content}")
        conversation_log = "\n".join(log_parts)
        # Handle case with no valid turns
        if not conversation_log: return "今回のセッションでは、レビューを作成するのに十分な対話がありませんでした。"

        # Prompt for Gemini to generate review
        prompt = f"""あなたはプロの英語教師です。以下の生徒との英会話ログを分析し、学習内容をまとめたレビューを作成してください。
# 指示
1.  **会話の簡単な要約**: どのようなトピックについて話したか、1〜2文で簡潔にまとめてください。
2.  **重要フレーズ**: 今回の会話で使われた、または学ぶべき重要な英単語やフレーズを3〜5個選んでください。**必ず `### 重要フレーズ` という見出しの下に、英語のフレーズのみを箇条書き (`- Phrase/Word`) で記述してください。** 各フレーズの説明や日本語訳は、その後のセクションで記述してください。
3.  **文法・表現の改善点**: 生徒の英語で改善できる点があれば、1〜2点指摘し、より自然な表現や正しい文法を提案してください。もし大きな間違いがなければ、その旨を記載してください。
4.  **全体的なフィードバック**: 全体をMarkdown形式で、生徒を励ますようなポジティブなトーンで記述してください。
# 会話ログ
{conversation_log}
"""
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            # --- Enhanced Response Handling ---
            if response and hasattr(response, 'text') and response.text:
                return response.text.strip()
            else:
                # Check for blocking or other issues
                candidates = getattr(response, 'candidates', [])
                if candidates and hasattr(candidates[0], 'finish_reason'):
                     reason = getattr(candidates[0], 'finish_reason', 'Unknown')
                     safety = getattr(candidates[0], 'safety_ratings', [])
                     logging.warning(f"レビュー生成が停止しました。理由: {reason}, 安全評価: {safety}")
                     return f"レビューの生成が停止されました（理由: {reason}）。"
                else:
                    # Log unexpected response structure
                    logging.warning(f"レビュー生成APIからの応答が不正または空です: {response}")
                    return "レビューの生成に失敗しました（APIからの応答が不正または空です）。"
            # --- End Enhanced Handling ---
        except Exception as e:
            logging.error(f"レビュー生成中にエラーが発生しました: {e}", exc_info=True)
            return f"レビューの生成中にエラーが発生しました: {type(e).__name__}"

    # --- _save_chat_log_to_obsidian ---
    async def _save_chat_log_to_obsidian(self, user: discord.User, history: list, review: str):
        # Check prerequisites
        if not self.dbx or not self.dropbox_vault_path:
             logging.warning("Obsidianへのログ保存をスキップ: DropboxクライアントまたはVaultパスが未設定です。"); return

        # Prepare filenames and timestamps
        now = datetime.now(JST); date_str = now.strftime('%Y-%m-%d'); timestamp = now.strftime('%Y%m%d%H%M%S')
        title = f"英会話ログ {user.display_name} {date_str}"
        # Sanitize filename components
        safe_title_part = re.sub(r'[\\/*?:"<>|]', '_', f"{user.display_name}_{date_str}")
        filename = f"{timestamp}-英会話ログ_{safe_title_part}.md"

        # Format conversation log
        log_parts = []
        for t in history:
            role = getattr(t, 'role', 'unknown')
            parts = getattr(t, 'parts', [])
            text_content = " ".join(getattr(p, 'text', '') for p in parts)
            if role in ['user', 'model'] and text_content:
                log_parts.append(f"- **{'You' if role == 'user' else 'AI'}:** {text_content}")
        conversation_log = "\n".join(log_parts)

        # Create Markdown content
        note_content = (f"# {title}\n\n- Date: {date_str}\n- Participant: {user.display_name}\n\n[[{date_str}]]\n\n"
                        f"---\n\n## 💬 Session Review\n{review}\n\n---\n\n## 📜 Full Transcript\n{conversation_log}\n")
        # Construct full path in Obsidian vault
        note_path = f"{self.dropbox_vault_path}{ENGLISH_LOG_PATH}/{filename}"

        try:
            # Upload log file to Dropbox (async)
            await asyncio.to_thread(self.dbx.files_upload, note_content.encode('utf-8'), note_path, mode=WriteMode('add'))
            logging.info(f"英会話ログ保存成功: {note_path}")

            # --- Add link to Daily Note ---
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"; daily_note_content = ""
            # Download daily note content (async)
            try:
                metadata, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                daily_note_content = res.content.decode('utf-8')
            except ApiError as e:
                # Handle case where daily note doesn't exist
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    daily_note_content = f"# {date_str}\n"; logging.info(f"デイリーノートが見つからないため新規作成: {daily_note_path}")
                else: raise # Re-raise other API errors

            # Prepare link format
            note_filename_for_link = filename.replace('.md', ''); link_path_part = ENGLISH_LOG_PATH.lstrip('/')
            # Create link display name (optional, customize as needed)
            link_display_name = f"英会話ログ ({user.display_name})" # Example display name
            link_to_add = f"- [[{link_path_part}/{note_filename_for_link}|{link_display_name}]]" # Use display name

            # Update daily note content using utility function
            new_daily_content = update_section(daily_note_content, link_to_add, DAILY_NOTE_ENGLISH_LOG_HEADER)
            # Upload updated daily note (async)
            await asyncio.to_thread(self.dbx.files_upload, new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
            logging.info(f"デイリーノート ({daily_note_path}) に英会話ログリンク追記成功。")
            # --- End Daily Note Update ---

        except AuthError as e: logging.error(f"英会話ログ保存/デイリーノート更新 Dropbox認証エラー: {e}")
        except ApiError as e: logging.error(f"英会話ログ保存/デイリーノート更新 Dropbox APIエラー: {e}", exc_info=True)
        except Exception as e: logging.error(f"英会話ログ保存/デイリーノート更新 予期せぬエラー: {e}", exc_info=True)

    # --- _save_sakubun_log_to_obsidian ---
    async def _save_sakubun_log_to_obsidian(self, japanese_question: str, user_answer: str, feedback_text: str):
        # Check prerequisites
        if not self.dbx or not self.dropbox_vault_path:
             logging.warning("瞬間英作文ログのObsidian保存をスキップ: DropboxクライアントまたはVaultパスが未設定です。"); return

        # Prepare filenames and timestamps
        now = datetime.now(JST); date_str = now.strftime('%Y-%m-%d'); timestamp = now.strftime('%Y%m%d%H%M%S')
        # Sanitize filename based on question
        safe_title_part = re.sub(r'[\\/*?:"<>|]', '_', japanese_question[:20]); filename = f"{timestamp}-Sakubun_{safe_title_part}.md"

        # --- Extract Model Answers from feedback ---
        # Adjusted regex to better find Model Answer section
        model_answers_match = re.search(r"^\#+\s*Model Answer(?:s)?\s*?\n+((?:^\s*[-*+].*(?:\n|$))+)", feedback_text, re.DOTALL | re.MULTILINE | re.IGNORECASE)
        model_answers = ""
        if model_answers_match:
            # Extract bullet points more reliably
            raw_answers = re.findall(r"^\s*[-*+]\s+(.+)", model_answers_match.group(1), re.MULTILINE)
            model_answers = "\n".join([f"- {ans.strip()}" for ans in raw_answers if ans.strip()])
        # --- End Model Answer Extraction ---

        # Create Markdown content
        note_content = (f"# {date_str} 瞬間英作文\n\n- Date: [[{date_str}]]\n---\n\n## 問題\n{japanese_question}\n\n"
                        f"## あなたの回答\n{user_answer}\n\n## AIによるフィードバック\n{feedback_text}\n")
        # Add Model Answers section if found
        if model_answers: note_content += f"---\n\n## モデルアンサー\n{model_answers}\n" # Add model answers if found
        # Construct full path in Obsidian vault
        note_path = f"{self.dropbox_vault_path}{SAKUBUN_LOG_PATH}/{filename}"

        try:
            # Upload log file to Dropbox (async)
            await asyncio.to_thread(self.dbx.files_upload, note_content.encode('utf-8'), note_path, mode=WriteMode('add'))
            logging.info(f"瞬間英作文ログ保存成功: {note_path}")

            # --- Add link to Daily Note ---
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"; daily_note_content = ""
            # Download daily note content (async)
            try:
                metadata, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                daily_note_content = res.content.decode('utf-8')
            except ApiError as e:
                # Handle case where daily note doesn't exist
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    daily_note_content = f"# {date_str}\n"; logging.info(f"デイリーノートが見つからないため新規作成: {daily_note_path}")
                else: raise # Re-raise other API errors

            # Prepare link format
            note_filename_for_link = filename.replace('.md', ''); link_path_part = SAKUBUN_LOG_PATH.lstrip('/')
            # Use truncated question as display name
            link_to_add = f"- [[{link_path_part}/{note_filename_for_link}|{japanese_question[:30]}...]]"
            # Update daily note content using utility function
            new_daily_content = update_section(daily_note_content, link_to_add, DAILY_NOTE_SAKUBUN_LOG_HEADER)
            # Upload updated daily note (async)
            await asyncio.to_thread(self.dbx.files_upload, new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
            logging.info(f"デイリーノート ({daily_note_path}) に瞬間英作文ログリンク追記成功。")
            # --- End Daily Note Update ---

        except AuthError as e: logging.error(f"瞬間英作文ログ保存/デイリーノート更新 Dropbox認証エラー: {e}")
        except ApiError as e: logging.error(f"瞬間英作文ログ保存/デイリーノート更新 Dropbox APIエラー: {e}", exc_info=True)
        except Exception as e: logging.error(f"瞬間英作文ログ保存/デイリーノート更新 予期せぬエラー: {e}", exc_info=True)


    # --- end_chat Command ---
    @app_commands.command(name="end", description="英会話を終了します")
    async def end_chat(self, interaction: discord.Interaction):
        if not self.is_ready:
             await interaction.response.send_message("英会話機能は現在利用できません（設定確認中）。", ephemeral=True); return
        # Check channel
        if interaction.channel_id != self.channel_id:
             await interaction.response.send_message(f"このコマンドは英会話チャンネル (<#{self.channel_id}>) でのみ利用できます。", ephemeral=True); return

        user_id = interaction.user.id
        session_path = self._get_session_path(user_id)
        # Remove session from active cache
        chat_session = self.chat_sessions.pop(user_id, None)

        # Check if session existed
        if not chat_session:
             await interaction.response.send_message("アクティブなセッションが見つかりませんでした。", ephemeral=True); return

        await interaction.response.defer() # Defer before long operations (review, save)

        review_text = "レビューの生成に失敗しました。"
        history_to_save = []
        important_phrases = []

        # Process history if session object has it
        if hasattr(chat_session, 'history'):
            history_to_save = chat_session.history
            try:
                logging.info(f"Generating review for user {user_id}...")
                review_text = await self._generate_chat_review(history_to_save)
                logging.info(f"Review generated for user {user_id}.")
                # Extract phrases for TTS
                important_phrases = extract_phrases_from_markdown_list(review_text, "重要フレーズ")

                # --- Save review to Google Docs ---
                if google_docs_enabled:
                    try:
                        await append_text_to_doc_async(
                            text_to_append=review_text,
                            source_type="English Chat Review",
                            title=f"English Review - {interaction.user.display_name} - {datetime.now(JST).strftime('%Y-%m-%d')}"
                        )
                        logging.info(f"Review saved to Google Docs for user {user_id}.")
                    except Exception as e_gdoc:
                        logging.error(f"Failed to save review to Google Docs for user {user_id}: {e_gdoc}", exc_info=True)
                # --- End Google Docs Save ---

                # Save chat log and review to Obsidian (includes daily note update)
                # Check if Dropbox is available before saving
                if self.dbx:
                    await self._save_chat_log_to_obsidian(interaction.user, history_to_save, review_text)
                else:
                    logging.warning(f"Dropbox not available, skipping Obsidian log save for user {user_id}.")

            except Exception as e:
                 logging.error(f"Error saving session/generating review for user {user_id} on end: {e}", exc_info=True)
                 # Notify user about the error, using followup since deferred
                 try: await interaction.followup.send("セッション履歴の保存またはレビュー生成中にエラーが発生しました。", ephemeral=True)
                 except discord.HTTPException: pass # Ignore if followup fails

        # Display review in Discord embed
        review_embed = discord.Embed(
            title="💬 Conversation Review",
            description=review_text[:4000], # Limit description length for embed
            color=discord.Color.gold(),
            timestamp=datetime.now(JST)
        ).set_footer(text=f"{interaction.user.display_name}'s session")

        # Create TTS View only if phrases exist and OpenAI client is available
        view = TTSView(important_phrases, self.openai_client) if important_phrases and self.openai_client else None

        # --- Send review embed (Corrected) ---
        try:
            # Use keyword arguments for clarity and correctness
            if view:
                await interaction.followup.send(embed=review_embed, view=view)
            else:
                await interaction.followup.send(embed=review_embed) # Send without view if None
        # --- Correction End ---
        except discord.HTTPException as e:
             logging.error(f"Failed to send review embed: {e}")
             # Fallback to sending text if embed fails
             try:
                 # Conditionally include view in fallback
                 fallback_kwargs = {"view": view} if view else {}
                 await interaction.followup.send(f"**Conversation Review:**\n{review_text[:1900]}", **fallback_kwargs) # Limit text length
             except discord.HTTPException as e2:
                 logging.error(f"Failed to send fallback review text: {e2}")
                 # Final fallback if even text fails
                 await interaction.followup.send("レビューの表示に失敗しました。ログを確認してください。", ephemeral=True)

        # Delete the session file from Dropbox after processing
        if self.dbx:
            try:
                logging.info(f"Attempting to delete session file: {session_path}")
                # Use asyncio.to_thread for Dropbox call
                await asyncio.to_thread(self.dbx.files_delete_v2, session_path)
                logging.info(f"Successfully deleted session file: {session_path}")
            except AuthError as e:
                 logging.error(f"Dropbox AuthError deleting session ({session_path}): {e}")
                 await interaction.followup.send("Dropbox認証エラーのため、セッションファイルの削除に失敗しました。", ephemeral=True)
            except ApiError as e:
                # Handle "not found" gracefully during deletion
                if isinstance(e.error, dropbox.exceptions.PathLookupError) and e.error.is_not_found():
                     logging.warning(f"Session file not found during deletion: {session_path}")
                else:
                    # Log other API errors and notify user
                    logging.error(f"セッションファイル削除失敗 ({session_path}): {e}")
                    await interaction.followup.send("セッションファイルの削除に失敗しました。", ephemeral=True)
            except Exception as e:
                logging.error(f"英会話終了エラー (ファイル削除中): {e}", exc_info=True)
                # Avoid sending another followup if one might have already failed
                logging.warning("セッション終了処理中にエラーが発生しました（ファイル削除）。") # Log instead of sending another message
        else:
             logging.warning("Dropbox client not available, skipping session file deletion.")


    # --- on_message Listener ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore messages if cog not ready, from bots, not in the designated channel, or starting with '/'
        if (not self.is_ready or
                message.author.bot or
                message.channel.id != self.channel_id or
                message.content.startswith('/')):
             return

        user_id = message.author.id

        # --- Handle Sakubun Answer (Check for reply) ---
        if message.reference and message.reference.message_id:
            try:
                original_msg = await message.channel.fetch_message(message.reference.message_id)
                # Check if it's a reply to the bot's Sakubun question embed
                # >>>>>>>>>>>>>>>>>> MODIFICATION START <<<<<<<<<<<<<<<<<<
                if (original_msg.author.id == self.bot.user.id and
                        original_msg.embeds and
                        "問" in original_msg.embeds[0].title and # タイトルに「問」があるか
                        original_msg.embeds[0].footer and # フッターが存在するか確認
                        original_msg.embeds[0].footer.text == "このメッセージに返信する形で英訳を投稿してください。"): # フッターの内容が一致するか
                # >>>>>>>>>>>>>>>>>> MODIFICATION END <<<<<<<<<<<<<<<<<<
                    await self.handle_sakubun_answer(message, message.content.strip(), original_msg)
                    return # Don't process as regular chat message if it's a Sakubun answer
            except discord.NotFound:
                logging.warning(f"Original message for Sakubun reply not found: {message.reference.message_id}")
            except Exception as e_ref:
                logging.error(f"Error processing potential Sakubun reply reference: {e_ref}")
        # --- End Sakubun Answer Handling ---

        # --- Handle Regular Chat Message ---
        if user_id in self.chat_sessions:
            chat = self.chat_sessions[user_id]
            async with message.channel.typing(): # Show typing indicator
                try:
                    logging.info(f"Sending message to Gemini for user {user_id}")
                    # Send user message to Gemini
                    response = await chat.send_message_async(message.content)
                    response_text = "Sorry, I couldn't generate a response." # Default text

                    # --- Process Gemini Response ---
                    if response and hasattr(response, 'text') and response.text:
                         response_text = response.text
                    elif response and hasattr(response, 'candidates') and response.candidates:
                         # Check if response was blocked or stopped for other reasons
                         candidate = response.candidates[0]
                         if hasattr(candidate, 'finish_reason') and candidate.finish_reason != 'STOP':
                             reason = candidate.finish_reason
                             safety = getattr(candidate, 'safety_ratings', [])
                             logging.warning(f"Gemini response blocked. Reason: {reason}, Safety: {safety}")
                             response_text = f"(Response blocked due to: {reason})" # Inform user
                         else: # No text but finish reason is STOP (unexpected)
                             logging.warning(f"Gemini response has no text but finish reason is STOP: {response}")
                    else: # Invalid response structure
                         logging.warning(f"Invalid response structure from Gemini: {response}")
                    # --- End Response Processing ---

                    logging.info(f"Received response from Gemini for user {user_id}")
                    # Create TTS view if client available
                    view = TTSView(response_text, self.openai_client) if self.openai_client else None
                    # Reply to user with AI response and TTS view
                    await message.reply(f"**AI:** {response_text}", view=view)

                    # Save session after successful interaction
                    await self._save_session_to_dropbox(user_id, chat.history)

                except Exception as e:
                    logging.error(f"英会話中のメッセージ処理エラー for user {user_id}: {e}", exc_info=True)
                    await message.reply("Sorry, an error occurred while processing your message.")
        # else: # User sent a message but no active session - ignore or send hint?
            # pass # Currently ignores messages if no session active

    # --- handle_sakubun_answer ---
    async def handle_sakubun_answer(self, message: discord.Message, user_answer: str, original_msg: discord.Message):
        if not self.is_ready:
            await message.reply("機能準備中です。") # Inform user if cog isn't ready
            return
        # Handle empty answers
        if not user_answer:
            await message.add_reaction("❓")
            await asyncio.sleep(5) # Wait a bit
            try:
                # Remove reaction after waiting
                await message.remove_reaction("❓", self.bot.user)
            except discord.HTTPException:
                logging.warning(f"リアクション❓の削除に失敗 (Message ID: {message.id})")
            return # Stop processing empty answer

        await message.add_reaction("🤔") # Indicate processing
        # Extract Japanese question from original embed
        japanese_question = original_msg.embeds[0].description.strip().replace("*","")

        # --- Updated Prompt for Sakubun Feedback ---
        prompt = f"""あなたはプロの英語教師です。以下の日本語の原文に対する学習者の英訳を添削し、フィードバックを提供してください。
# 指示
1.  **評価**: 学習者の英訳が良い点、改善できる点を具体的に評価してください。
2.  **改善案**: より自然な英語表現や文法的に正しい表現を1つ以上提案してください。
3.  **重要フレーズ**: フィードバックの中で特に重要な英単語やフレーズを3〜5個選んでください。**必ず `### 重要フレーズ` という見出しの下に、英語のフレーズのみを箇条書き (`- Phrase/Word`) で記述してください。**
4.  **モデルアンサー**: `### Model Answer` という見出しの下に、模範解答となる英文を2〜3個、箇条書き (`- Answer Sentence`) で提示してください。
5.  **文法・表現ポイント**: 関連する文法事項や表現のポイントがあれば簡潔に解説してください。
6.  **形式**: 全体をMarkdown形式で記述してください。
# 日本語の原文
{japanese_question}
# 学習者の英訳
{user_answer}"""
        # --- End Updated Prompt ---

        feedback_text = "フィードバック生成失敗。"
        view = None # Initialize view as None
        try:
            # Generate feedback using Gemini
            response = await self.gemini_model.generate_content_async(prompt)
            # Validate response
            if response and hasattr(response, 'text') and response.text: feedback_text = response.text
            else: logging.warning(f"Sakubun feedback response invalid: {response}")

            # Create feedback embed
            feedback_embed = discord.Embed(title=f"添削結果: 「{japanese_question}」", description=feedback_text[:4000], color=discord.Color.green()) # Limit description length

            # --- Extract phrases for TTS ---
            # Use the same helper function as chat review
            important_phrases = extract_phrases_from_markdown_list(feedback_text, "重要フレーズ")
            # --- End phrase extraction ---

            # Create TTS view if phrases found and client available
            if important_phrases and self.openai_client:
                view = TTSView(important_phrases, self.openai_client)

            # Reply with embed and TTS view
            await message.reply(embed=feedback_embed, view=view)

            # --- Save log to Obsidian ---
            await self._save_sakubun_log_to_obsidian(japanese_question, user_answer, feedback_text) # Save log
            # --- End Obsidian Save ---

            # --- Save log to Google Docs ---
            if google_docs_enabled:
                 try:
                    gdoc_content = f"## 問題\n{japanese_question}\n\n## 回答\n{user_answer}\n\n## フィードバック\n{feedback_text}"
                    await append_text_to_doc_async(
                        text_to_append=gdoc_content,
                        source_type="Sakubun Log",
                        title=f"Sakubun - {japanese_question[:30]}... - {datetime.now(JST).strftime('%Y-%m-%d')}"
                    )
                    logging.info("Sakubun log saved to Google Docs.")
                 except Exception as e_gdoc_sakubun:
                      logging.error(f"Failed to save Sakubun log to Google Docs: {e_gdoc_sakubun}", exc_info=True)
            # --- End Google Docs Save ---

        except Exception as e_fb:
            logging.error(f"瞬間英作文フィードバック/保存エラー: {e_fb}", exc_info=True)
            await message.reply("フィードバック処理中にエラーが発生しました。")
        finally: # Ensure reaction is removed
             try:
                 await message.remove_reaction("🤔", self.bot.user)
             except discord.HTTPException:
                 pass # Ignore if already removed or other error


# --- setup Function ---
async def setup(bot):
    # Retrieve necessary API keys and tokens from environment variables
    openai_key = os.getenv("OPENAI_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")
    dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
    dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
    dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
    channel_id = os.getenv("ENGLISH_LEARNING_CHANNEL_ID")

    # Check required environment variables for the cog to function
    if not all([gemini_key, dropbox_refresh_token, dropbox_app_key, dropbox_app_secret, channel_id]):
        logging.error("EnglishLearningCog: 必須の環境変数 (GEMINI_API_KEY, DROPBOX_REFRESH_TOKEN, DROPBOX_APP_KEY, DROPBOX_APP_SECRET, ENGLISH_LEARNING_CHANNEL_ID) が不足しているため、Cogをロードしません。")
        return # Do not load the cog if requirements aren't met

    # Validate channel ID format
    try:
        channel_id_int = int(channel_id) # Ensure channel ID is integer
    except ValueError:
        logging.error("EnglishLearningCog: ENGLISH_LEARNING_CHANNEL_ID must be a valid integer.")
        return # Do not load if channel ID is invalid

    # Pass credentials to the Cog's __init__ method during instantiation
    cog_instance = EnglishLearningCog(
        bot,
        openai_key, # OpenAI key is optional for TTS, can be None
        gemini_key,
        dropbox_refresh_token,
        dropbox_app_key,
        dropbox_app_secret
    )
    # Only add the cog to the bot if it initialized successfully (is_ready is True)
    if cog_instance.is_ready:
        await bot.add_cog(cog_instance)
        logging.info("EnglishLearningCog loaded successfully.")
    else:
        # Log error if initialization failed (usually due to missing keys or Dropbox connection failure)
        logging.error("EnglishLearningCog failed to initialize and was not loaded.")