import os
import json
import asyncio
import logging
import discord
from discord.ext import commands
from discord import app_commands
from openai import AsyncOpenAI
import google.generativeai as genai
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError, AuthError # Import AuthError
import io
import re

# --- UI Component: TTSView ---
# [TTSView Class Code - unchanged from previous correction]
class TTSView(discord.ui.View):
    MAX_BUTTONS = 5 # 表示するボタンの最大数

    def __init__(self, phrases_or_text: list[str] | str, openai_client):
        super().__init__(timeout=3600)
        self.openai_client = openai_client
        self.phrases = []

        if isinstance(phrases_or_text, str):
            clean_text = re.sub(r'<@!?\d+>', '', phrases_or_text)
            clean_text = re.sub(r'[*_`~#]', '', clean_text)
            full_text = clean_text.strip()[:2000]
            if full_text:
                self.phrases.append(full_text)
                label = (full_text[:25] + '...') if len(full_text) > 28 else full_text
                button = discord.ui.Button(
                    label=f"🔊 {label}", style=discord.ButtonStyle.secondary, custom_id="tts_phrase_0"
                )
                button.callback = self.tts_button_callback
                self.add_item(button)
        elif isinstance(phrases_or_text, list):
            self.phrases = phrases_or_text[:self.MAX_BUTTONS]
            for index, phrase in enumerate(self.phrases):
                clean_phrase = re.sub(r'[*_`~#]', '', phrase.strip())[:2000]
                if not clean_phrase: continue
                label = (clean_phrase[:25] + '...') if len(clean_phrase) > 28 else clean_phrase
                button = discord.ui.Button(
                    label=f"🔊 {label}", style=discord.ButtonStyle.secondary,
                    custom_id=f"tts_phrase_{index}", row=index // 5
                )
                button.callback = self.tts_button_callback
                self.add_item(button)

    async def tts_button_callback(self, interaction: discord.Interaction):
        custom_id = interaction.data.get("custom_id")
        logging.info(f"TTSボタンクリック: {custom_id} by {interaction.user}")
        if not custom_id or not custom_id.startswith("tts_phrase_"):
            # Use followup if response already sent/deferred
            if interaction.response.is_done():
                await interaction.followup.send("無効なボタンIDです。", ephemeral=True, delete_after=10)
            else:
                await interaction.response.send_message("無効なボタンIDです。", ephemeral=True, delete_after=10)
            return
        try:
            phrase_index = int(custom_id.split("_")[-1])
            if not (0 <= phrase_index < len(self.phrases)):
                 if interaction.response.is_done():
                    await interaction.followup.send("無効なフレーズインデックスです。", ephemeral=True, delete_after=10)
                 else:
                    await interaction.response.send_message("無効なフレーズインデックスです。", ephemeral=True, delete_after=10)
                 return

            phrase_to_speak = self.phrases[phrase_index]
            if not phrase_to_speak:
                 if interaction.response.is_done():
                    await interaction.followup.send("空のフレーズは読み上げできません。", ephemeral=True, delete_after=10)
                 else:
                    await interaction.response.send_message("空のフレーズは読み上げできません。", ephemeral=True, delete_after=10)
                 return
            if not self.openai_client:
                 if interaction.response.is_done():
                    await interaction.followup.send("TTS機能が設定されていません (OpenAI APIキー未設定)。", ephemeral=True, delete_after=10)
                 else:
                    await interaction.response.send_message("TTS機能が設定されていません (OpenAI APIキー未設定)。", ephemeral=True, delete_after=10)
                 return

            # Defer only if not already done
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=True)

            response = await self.openai_client.audio.speech.create(
                model="tts-1", voice="alloy", input=phrase_to_speak, response_format="mp3"
            )
            audio_bytes = response.content
            audio_buffer = io.BytesIO(audio_bytes)
            audio_file = discord.File(fp=audio_buffer, filename=f"phrase_{phrase_index}.mp3")
            # Always use followup after deferring
            await interaction.followup.send(f"🔊 \"{phrase_to_speak}\"", file=audio_file, ephemeral=True)
        except ValueError:
            logging.error(f"custom_idからインデックスの解析に失敗: {custom_id}")
            # Always use followup after deferring
            await interaction.followup.send("ボタン処理エラー。", ephemeral=True)
        except openai.APIError as e:
             logging.error(f"OpenAI APIエラー (TTS生成中): {e}", exc_info=True)
             await interaction.followup.send(f"音声生成中にOpenAI APIエラーが発生しました: {e}", ephemeral=True)
        except Exception as e:
            logging.error(f"tts_button_callback内でエラー: {e}", exc_info=True)
            # Always use followup after deferring
            await interaction.followup.send(f"音声の生成・送信中にエラーが発生しました: {e}", ephemeral=True)
# --- End TTSView ---

# Cog definition
class EnglishLearning(commands.Cog):
    # --- __init__ ---
    def __init__(self, bot, openai_api_key, gemini_api_key, dropbox_refresh_token, dropbox_app_key, dropbox_app_secret):
        self.bot = bot
        self.openai_client = AsyncOpenAI(api_key=openai_api_key) if openai_api_key else None
        genai.configure(api_key=gemini_api_key)
        self.model = genai.GenerativeModel("gemini-2.5-pro")
        self.dropbox_refresh_token = dropbox_refresh_token
        self.dropbox_app_key = dropbox_app_key
        self.dropbox_app_secret = dropbox_app_secret
        self.dbx = None # Initialize later
        self.session_dir = "/english_sessions"
        self.chat_sessions = {} # Initialize chat_sessions dictionary
        self.is_ready = False # Set readiness after initialization checks

        # Initialize Dropbox client and set readiness
        if dropbox_refresh_token and dropbox_app_key and dropbox_app_secret:
            try:
                self.dbx = dropbox.Dropbox(
                    app_key=self.dropbox_app_key,
                    app_secret=self.dropbox_app_secret,
                    oauth2_refresh_token=self.dropbox_refresh_token
                )
                # Test connection (optional but recommended)
                self.dbx.users_get_current_account()
                self.is_ready = True # Ready only if Dropbox is ok
                logging.info("Dropbox client initialized successfully for EnglishLearningCog.")
            except AuthError as e:
                logging.error(f"Dropbox AuthError during initialization for EnglishLearningCog: {e}. Cog might not function fully.")
                self.dbx = None # Ensure dbx is None if auth fails
            except Exception as e:
                logging.error(f"Failed to initialize Dropbox client for EnglishLearningCog: {e}", exc_info=True)
                self.dbx = None
        else:
            logging.warning("Dropbox credentials missing. Session saving/loading will be disabled.")
            self.dbx = None # Ensure dbx is None

        if not self.openai_client:
            logging.warning("OpenAI API Key not found. TTS functionality will be disabled.")
        if not self.dbx:
            logging.warning("Dropbox client failed to initialize or missing credentials. Session persistence disabled.")

        logging.info("EnglishLearning Cog initialization attempt finished.")
        # Final readiness check
        if not gemini_api_key:
             logging.error("Gemini API key missing. Cog cannot function.")
             self.is_ready = False
        elif not self.dbx:
             logging.warning("Dropbox not available, disabling session persistence but core chat might work.")
             # Allow is_ready = True if only persistence fails, but log it clearly
             self.is_ready = True # Or False depending on whether persistence is critical

    # --- _get_session_path ---
    def _get_session_path(self, user_id: int) -> str:
        return f"{self.session_dir}/{user_id}.json"

    # --- english_chat Command ---
    @app_commands.command(name="english_chat", description="AIと英会話を始めます")
    async def english_chat(self, interaction: discord.Interaction):
        if not self.is_ready or not self.dbx: # Check dbx specifically for session features
             await interaction.response.send_message("英会話セッション機能は現在利用できません（Dropbox設定不足）。", ephemeral=True)
             return
        # Add check if user already has a session
        if interaction.user.id in self.chat_sessions:
             await interaction.response.send_message("既にセッションを開始しています。終了は `/end`。", ephemeral=True)
             return

        await interaction.response.defer()
        user_id = interaction.user.id
        session_path = self._get_session_path(user_id)
        session = await self._load_session_from_dropbox(user_id) # Returns None on failure or not found

        system_instruction = "..." # Keep the system instruction
        model_with_instruction = genai.GenerativeModel("gemini-2.5-pro", system_instruction=system_instruction)

        chat_session = None # Initialize chat_session variable

        if session is not None:
            logging.info(f"セッション再開: {session_path}")
            try:
                chat_session = model_with_instruction.start_chat(history=session)
                # Send a continuation message
                response = await asyncio.wait_for(chat_session.send_message_async("Welcome back! Let's continue our English conversation. How have you been?"), timeout=60)
                response_text = response.text if response and hasattr(response, "text") else "Hi again! Let's chat."
            except Exception as e:
                logging.error(f"Error resuming chat session for {user_id}: {e}", exc_info=True)
                # Fallback to starting a new session if resuming fails
                chat_session = model_with_instruction.start_chat(history=[])
                response_text = "Sorry, I had trouble resuming our last session. Let's start fresh! How are you?"
        else:
            logging.info(f"新規セッション開始: {session_path}")
            chat_session = model_with_instruction.start_chat(history=[])
            initial_prompt = "Hi! I'm your AI English partner. Let's chat! How's it going?"
            try:
                response = await asyncio.wait_for(chat_session.send_message_async(initial_prompt), timeout=60)
                response_text = response.text if response and hasattr(response, "text") else "Hi! Let's chat."
            except asyncio.TimeoutError:
                logging.error("初回応答タイムアウト")
                response_text = "Sorry, the initial response timed out. Let's start anyway. How are you?"
            except Exception as e_init:
                logging.error(f"初回応答生成失敗: {e_init}", exc_info=True)
                response_text = "Sorry, an error occurred while starting the chat. Let's try starting simply. How are you?"

        # Store the chat session object
        if chat_session:
            self.chat_sessions[user_id] = chat_session
        else:
             await interaction.followup.send("チャットセッションを開始できませんでした。", ephemeral=True)
             return # Stop if chat session couldn't be created

        # Send the AI's first message with TTS view
        view = TTSView(response_text, self.openai_client) if self.openai_client else None
        await interaction.followup.send(f"**AI:** {response_text}", view=view)

        # Send ephemeral follow-up without delete_after
        try:
            # Remove delete_after argument - it's not supported here
            await interaction.followup.send("会話を続けるには、このメッセージに返信してください。終了は `/end`", ephemeral=True)
        except discord.HTTPException as e:
             logging.warning(f"Failed to send ephemeral followup for english_chat start: {e}")
        except TypeError as e:
             # Catching the specific TypeError seen in logs
             logging.error(f"TypeError sending ephemeral followup for english_chat start: {e}. Check discord.py version compatibility.", exc_info=True)
             # Try sending without ephemeral as a fallback? Or just log.
        except Exception as e:
             logging.error(f"Unexpected error sending ephemeral followup: {e}", exc_info=True)


    # --- _load_session_from_dropbox ---
    async def _load_session_from_dropbox(self, user_id: int) -> list | None:
        if not self.dbx: return None # Check if dbx is initialized
        session_path = self._get_session_path(user_id)
        try:
            logging.info(f"Loading session from: {session_path}")
            metadata, res = await asyncio.to_thread(self.dbx.files_download, session_path)
            loaded_data = json.loads(res.content)
            # Convert loaded data to Gemini's expected history format
            history = []
            for item in loaded_data:
                role = item.get("role")
                parts_list = item.get("parts", [])
                # Ensure parts_list is actually a list of strings
                if role and isinstance(parts_list, list) and all(isinstance(p, str) for p in parts_list):
                     # Reconstruct the parts structure expected by Gemini
                     gemini_parts = [{"text": text} for text in parts_list]
                     history.append({"role": role, "parts": gemini_parts})
                else:
                     logging.warning(f"Skipping invalid history item for user {user_id}: {item}")
            logging.info(f"Successfully loaded and formatted session for user {user_id}")
            return history
        except AuthError as e: # Catch AuthError specifically
            logging.error(f"Dropbox AuthError loading session ({session_path}): {e}. Check token validity.")
            # Optionally try to re-initialize dbx here, but might be complex
            return None
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                logging.info(f"Session file not found for {user_id} at {session_path}")
                return None
            logging.error(f"Dropbox APIエラー ({session_path}): {e}")
            return None
        except json.JSONDecodeError as json_e:
            logging.error(f"JSON解析失敗 ({session_path}): {json_e}")
            return None
        except Exception as e:
            logging.error(f"セッション読込エラー ({session_path}): {e}", exc_info=True)
            return None

    # --- _save_session_to_dropbox ---
    async def _save_session_to_dropbox(self, user_id: int, history: list):
        if not self.dbx: return # Check if dbx is initialized
        session_path = self._get_session_path(user_id)
        try:
            serializable_history = []
            for turn in history:
                role = getattr(turn, "role", None)
                parts = getattr(turn, "parts", [])
                if role and parts:
                    part_texts = [getattr(p, "text", str(p)) for p in parts]
                    serializable_history.append({"role": role, "parts": part_texts})
            if not serializable_history:
                 logging.warning(f"History for user {user_id} is empty or not serializable. Skipping save.")
                 return
            content = json.dumps(serializable_history, ensure_ascii=False, indent=2).encode("utf-8")
            await asyncio.to_thread(
                self.dbx.files_upload, content, session_path, mode=WriteMode("overwrite")
            )
            logging.info(f"Saved session to: {session_path}")
        except AuthError as e: # Catch AuthError specifically
             logging.error(f"Dropbox AuthError saving session ({session_path}): {e}. Check token validity.")
        except Exception as e:
            logging.error(f"セッション保存失敗 ({session_path}): {e}", exc_info=True)

    # --- end_chat Command ---
    @app_commands.command(name="end", description="英会話を終了します")
    async def end_chat(self, interaction: discord.Interaction):
        if not self.is_ready or not self.dbx: # Check dbx specifically for session features
             await interaction.response.send_message("英会話セッション機能は現在利用できません（Dropbox設定不足）。", ephemeral=True)
             return

        user_id = interaction.user.id
        session_path = self._get_session_path(user_id)
        chat_session = self.chat_sessions.pop(user_id, None) # Remove session from memory

        if not chat_session:
             await interaction.response.send_message("アクティブなセッションが見つかりませんでした。", ephemeral=True)
             return

        await interaction.response.defer() # Defer response

        # Generate review and save history before deleting the file
        review = "Review generation skipped for now." # Placeholder
        history_to_save = []
        if hasattr(chat_session, 'history'):
            history_to_save = chat_session.history
            try:
                 # Generate review (implement _generate_chat_review if needed)
                 # review = await self._generate_chat_review(history_to_save)
                 # Save history to Dropbox
                 await self._save_session_to_dropbox(user_id, history_to_save)
                 # Optionally save review/log to Obsidian here
            except Exception as e:
                 logging.error(f"Error saving session/generating review for user {user_id} on end: {e}", exc_info=True)
                 await interaction.followup.send("セッション履歴の保存またはレビュー生成中にエラーが発生しました。", ephemeral=True)
                 # Continue to delete file anyway? Or return? Let's continue for now.

        # Now delete the file from Dropbox
        try:
            logging.info(f"Attempting to delete session file: {session_path}")
            await asyncio.to_thread(self.dbx.files_delete_v2, session_path)
            await interaction.followup.send(f"セッションファイルを削除し、会話を終了しました。\n**レビュー:**\n{review}") # Send review
        except AuthError as e: # Catch AuthError specifically
             logging.error(f"Dropbox AuthError deleting session ({session_path}): {e}. Check token validity.")
             await interaction.followup.send("Dropbox認証エラーのため、セッションファイルの削除に失敗しました。")
        except ApiError as e:
            if isinstance(e.error, dropbox.exceptions.PathLookupError) and e.error.is_not_found():
                 logging.warning(f"Session file not found during deletion: {session_path}")
                 await interaction.followup.send("セッションファイルが見つからず削除できませんでした（既に削除済みかもしれません）。会話は終了しました。") # Adjust message
            else:
                logging.error(f"セッション削除失敗 ({session_path}): {e}")
                await interaction.followup.send("セッションファイルの削除に失敗しました。")
        except Exception as e:
            logging.error(f"英会話終了エラー: {e}", exc_info=True)
            await interaction.followup.send("セッション終了処理中にエラーが発生しました。")

    # --- on_message Listener (Implemented) ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if (not self.is_ready or
                message.author.bot or
                # Ensure channel ID is correctly read and compared as int
                message.channel.id != int(os.getenv("ENGLISH_LEARNING_CHANNEL_ID", 0)) or
                message.content.startswith('/')):
             return

        user_id = message.author.id
        if user_id not in self.chat_sessions:
            # Maybe send a hint? "Use /english_chat to start a conversation."
            # Or just ignore if no active session. Let's ignore for now.
            return

        chat = self.chat_sessions[user_id]
        async with message.channel.typing():
             try:
                logging.info(f"Sending message to Gemini for user {user_id}")
                # Use the existing chat session object
                response = await chat.send_message_async(message.content)
                response_text = response.text if response and hasattr(response, 'text') else "Sorry, I couldn't generate a response."
                logging.info(f"Received response from Gemini for user {user_id}")

                view = TTSView(response_text, self.openai_client) if self.openai_client else None
                await message.reply(f"**AI:** {response_text}", view=view)

                # Save session history after each turn (optional, can be performance intensive)
                # Consider saving only on /end or periodically
                # await self._save_session_to_dropbox(user_id, chat.history)

             except Exception as e:
                 logging.error(f"英会話中のメッセージ処理エラー for user {user_id}: {e}", exc_info=True)
                 await message.reply("Sorry, an error occurred while processing your message.")

# --- setup Function ---
async def setup(bot):
    openai_key = os.getenv("OPENAI_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")
    dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
    dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
    dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")

    if not all([gemini_key, dropbox_refresh_token, dropbox_app_key, dropbox_app_secret]):
        logging.error("EnglishLearningCog: 必須の環境変数 (GEMINI_API_KEY, DROPBOX_REFRESH_TOKEN, DROPBOX_APP_KEY, DROPBOX_APP_SECRET) が不足しているため、Cogをロードしません。")
        return

    # Pass credentials to the Cog's __init__
    await bot.add_cog(
        EnglishLearning(
            bot,
            openai_key,
            gemini_key,
            dropbox_refresh_token,
            dropbox_app_key,
            dropbox_app_secret
        )
    )