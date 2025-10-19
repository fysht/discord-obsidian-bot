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
        pattern = rf"^\#+\s*{re.escape(heading)}.*?\n((?:^\s*[-*+]\s+.*(?:\n|$))+)"
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
    MAX_BUTTONS = 5

    def __init__(self, phrases_or_text: list[str] | str, openai_client):
        super().__init__(timeout=3600)
        self.openai_client = openai_client
        self.phrases = []

        if isinstance(phrases_or_text, str):
            clean_text = re.sub(r'<@!?\d+>', '', phrases_or_text)
            clean_text = re.sub(r'[*_`~#]', '', clean_text)
            full_text = clean_text.strip()[:2000] # Limit length for API
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
                clean_phrase = re.sub(r'[*_`~#]', '', phrase.strip())[:2000] # Limit length
                if not clean_phrase: continue
                label = (clean_phrase[:25] + '...') if len(clean_phrase) > 28 else clean_phrase
                button = discord.ui.Button(
                    label=f"🔊 {label}", style=discord.ButtonStyle.secondary,
                    custom_id=f"tts_phrase_{index}", row=index // 5 # Basic row management
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


# --- Cog: EnglishLearningCog ---
class EnglishLearningCog(commands.Cog, name="EnglishLearning"):
    """瞬間英作文とAI壁打ちチャットによる英語学習を支援するCog"""

    # --- __init__ ---
    def __init__(self, bot: commands.Bot, openai_api_key, gemini_api_key, dropbox_refresh_token, dropbox_app_key, dropbox_app_secret):
        self.bot = bot
        self.openai_client = AsyncOpenAI(api_key=openai_api_key) if openai_api_key else None
        genai.configure(api_key=gemini_api_key)
        self.gemini_model = genai.GenerativeModel("gemini-2.5-pro") 
        self.dropbox_refresh_token = dropbox_refresh_token
        self.dropbox_app_key = dropbox_app_key
        self.dropbox_app_secret = dropbox_app_secret
        self.dbx = None
        self.session_dir = "/english_sessions" # Dropbox内のパス
        self.chat_sessions = {}
        self.is_ready = False
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.channel_id = int(os.getenv("ENGLISH_LEARNING_CHANNEL_ID", 0))
        self.sakubun_questions = []

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
        await self._load_sakubun_questions()
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
            path = f"{self.dropbox_vault_path}{SAKUBUN_NOTE_PATH}"
            logging.info(f"Loading Sakubun questions from: {path}")
            metadata, res = await asyncio.to_thread(self.dbx.files_download, path)
            content = res.content.decode('utf-8')
            questions = re.findall(r'^\s*-\s*(.+?)(?:\s*::\s*.*)?$', content, re.MULTILINE)
            if questions:
                self.sakubun_questions = [q.strip() for q in questions if q.strip()] # Filter empty questions
                logging.info(f"Obsidianから{len(self.sakubun_questions)}問の瞬間英作文の問題を読み込みました。")
            else:
                logging.warning(f"Obsidianのファイル ({SAKUBUN_NOTE_PATH}) に問題が見つかりませんでした (形式: '- 日本語文')。")
        except AuthError as e: logging.error(f"Dropbox AuthError loading Sakubun questions: {e}")
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                logging.warning(f"瞬間英作文ファイルが見つかりません: {path}")
            else: logging.error(f"Dropbox APIエラー (瞬間英作文読み込み): {e}")
        except Exception as e: logging.error(f"Obsidianからの問題読み込み中に予期せぬエラー: {e}", exc_info=True)

    # --- morning_sakubun_task, evening_sakubun_task ---
    @tasks.loop(time=MORNING_SAKUBUN_TIME)
    async def morning_sakubun_task(self):
        channel = self.bot.get_channel(self.channel_id)
        if channel: await self._run_sakubun_session(channel, 1, "朝")
        else: logging.error(f"Sakubun channel not found: {self.channel_id}")

    @tasks.loop(time=EVENING_SAKUBUN_TIME)
    async def evening_sakubun_task(self):
        channel = self.bot.get_channel(self.channel_id)
        if channel: await self._run_sakubun_session(channel, 1, "夜")
        else: logging.error(f"Sakubun channel not found: {self.channel_id}")

    # ループ開始前にBotの準備を待つ
    @morning_sakubun_task.before_loop
    @evening_sakubun_task.before_loop
    async def before_sakubun_tasks(self):
        await self.bot.wait_until_ready()
        logging.info("Sakubun tasks waiting for bot readiness...")


    async def _run_sakubun_session(self, channel: discord.TextChannel, num_questions: int, session_name: str):
        if not self.is_ready: return
        if not self.sakubun_questions:
            await channel.send("⚠️ 瞬間英作文の問題リストが空のため、出題できません。Obsidianのファイルを確認してください。"); return

        questions_to_ask = random.sample(self.sakubun_questions, min(num_questions, len(self.sakubun_questions)))

        embed = discord.Embed(
            title=f"✍️ 今日の{session_name}・瞬間英作文 ({len(questions_to_ask)}問)",
            description=f"これから{len(questions_to_ask)}問出題します。",
            color=discord.Color.purple()
        ).set_footer(text="約20秒後に最初の問題が出題されます。")
        await channel.send(embed=embed)
        await asyncio.sleep(20)

        for i, q_text in enumerate(questions_to_ask):
            q_embed = discord.Embed(
                title=f"第 {i+1} 問 / {len(questions_to_ask)} 問",
                description=f"**{q_text}**",
                color=discord.Color.blue()
            ).set_footer(text="このメッセージに返信する形で英訳を投稿してください。")
            await channel.send(embed=q_embed)
            if i < len(questions_to_ask) - 1:
                await asyncio.sleep(20)


    # --- /english command ---
    @app_commands.command(name="english", description="AIとの英会話チャットを開始または再開します。")
    async def english(self, interaction: discord.Interaction):
        if not self.is_ready:
             await interaction.response.send_message("英会話機能は現在利用できません（設定確認中）。", ephemeral=True); return
        if interaction.channel_id != self.channel_id:
             await interaction.response.send_message(f"このコマンドは英会話チャンネル (<#{self.channel_id}>) でのみ利用できます。", ephemeral=True); return
        if interaction.user.id in self.chat_sessions:
             await interaction.response.send_message("既にセッションを開始しています。終了は `/end`。", ephemeral=True); return

        await interaction.response.defer()
        user_id = interaction.user.id
        session_path = self._get_session_path(user_id)
        session = await self._load_session_from_dropbox(user_id) # Returns None if dbx unavailable or error

        system_instruction = "あなたはフレンドリーな英会話の相手です。ユーザーのメッセージに共感したり、質問を返したりして、会話を弾ませてください。もしユーザーの英語に文法的な誤りや不自然な点があれば、会話の流れを止めないように優しく指摘し、正しい表現を提案してください。例：「`I go to the park yesterday.` → `Oh, you went to the park yesterday! What did you do there?`」のように、自然な訂正を会話に含めてください。あなたの返答は、常に自然な英語で行ってください。"
        model_with_instruction = genai.GenerativeModel("gemini-2.5-pro", system_instruction=system_instruction)

        chat_session = None
        response_text = ""

        try:
            if session is not None:
                logging.info(f"セッション再開: {session_path}")
                chat_session = model_with_instruction.start_chat(history=session)
                response = await asyncio.wait_for(chat_session.send_message_async("Welcome back! Let's continue our English conversation. How have you been?"), timeout=60)
                response_text = response.text if response and hasattr(response, "text") else "Hi again! Let's chat."
            else:
                logging.info(f"新規セッション開始: {session_path}")
                chat_session = model_with_instruction.start_chat(history=[])
                initial_prompt = "Hi! I'm your AI English partner. Let's chat! How's it going?"
                response = await asyncio.wait_for(chat_session.send_message_async(initial_prompt), timeout=60)
                response_text = response.text if response and hasattr(response, "text") else "Hi! Let's chat."

        except asyncio.TimeoutError:
            logging.error(f"Chat start/resume timeout for user {user_id}")
            response_text = "Sorry, the response timed out. Let's try starting. How are you?"
            if chat_session is None: chat_session = model_with_instruction.start_chat(history=[])
        except Exception as e:
            logging.error(f"Error starting/resuming chat session for {user_id}: {e}", exc_info=True)
            response_text = "Sorry, an error occurred while starting our chat. Let's try simply. How are you?"
            if chat_session is None: chat_session = model_with_instruction.start_chat(history=[])

        if chat_session:
            self.chat_sessions[user_id] = chat_session
        else:
             await interaction.followup.send("チャットセッションを開始できませんでした。", ephemeral=True); return

        view = TTSView(response_text, self.openai_client) if self.openai_client else None
        await interaction.followup.send(f"**AI:** {response_text}", view=view)

        try:
            await interaction.followup.send("会話を続けるには、メッセージを送信してください。終了は `/end`", ephemeral=True)
        except Exception as e:
             logging.error(f"Unexpected error sending ephemeral followup: {e}", exc_info=True)

    # --- _load_session_from_dropbox ---
    async def _load_session_from_dropbox(self, user_id: int) -> list | None:
        if not self.dbx: return None
        session_path = self._get_session_path(user_id)
        try:
            logging.info(f"Loading session from: {session_path}")
            metadata, res = await asyncio.to_thread(self.dbx.files_download, session_path)
            loaded_data = json.loads(res.content)
            history = []
            for item in loaded_data:
                role = item.get("role")
                parts_list = item.get("parts", [])
                if role and isinstance(parts_list, list) and all(isinstance(p, str) for p in parts_list):
                     gemini_parts = [{"text": text} for text in parts_list]
                     history.append({"role": role, "parts": gemini_parts})
                else:
                     logging.warning(f"Skipping invalid history item for user {user_id}: {item}")
            logging.info(f"Successfully loaded and formatted session for user {user_id}")
            return history
        except AuthError as e: logging.error(f"Dropbox AuthError loading session ({session_path}): {e}. Check token validity."); return None
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found(): logging.info(f"Session file not found for {user_id} at {session_path}"); return None
            logging.error(f"Dropbox APIエラー ({session_path}): {e}"); return None
        except json.JSONDecodeError as json_e: logging.error(f"JSON解析失敗 ({session_path}): {json_e}"); return None
        except Exception as e: logging.error(f"セッション読込エラー ({session_path}): {e}", exc_info=True); return None

    # --- _save_session_to_dropbox ---
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
            if not serializable_history: logging.warning(f"History for user {user_id} is empty or not serializable. Skipping save."); return
            content = json.dumps(serializable_history, ensure_ascii=False, indent=2).encode("utf-8")
            await asyncio.to_thread(
                self.dbx.files_upload, content, session_path, mode=WriteMode("overwrite")
            )
            logging.info(f"Saved session to: {session_path}")
        except AuthError as e: logging.error(f"Dropbox AuthError saving session ({session_path}): {e}. Check token validity.")
        except Exception as e: logging.error(f"セッション保存失敗 ({session_path}): {e}", exc_info=True)

    # --- _generate_chat_review ---
    async def _generate_chat_review(self, history: list) -> str:
        log_parts = []
        for t in history:
            role = getattr(t, 'role', 'unknown')
            parts = getattr(t, 'parts', [])
            text_content = " ".join(getattr(p, 'text', '') for p in parts)
            if role in ['user', 'model'] and text_content:
                log_parts.append(f"**{'You' if role == 'user' else 'AI'}:** {text_content}")
        conversation_log = "\n".join(log_parts)
        if not conversation_log: return "今回のセッションでは、レビューを作成するのに十分な対話がありませんでした。"

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
            if response and hasattr(response, 'text') and response.text:
                return response.text.strip()
            else:
                candidates = getattr(response, 'candidates', [])
                if candidates and hasattr(candidates[0], 'finish_reason'):
                     reason = getattr(candidates[0], 'finish_reason', 'Unknown')
                     safety = getattr(candidates[0], 'safety_ratings', [])
                     logging.warning(f"レビュー生成が停止しました。理由: {reason}, 安全評価: {safety}")
                     return f"レビューの生成が停止されました（理由: {reason}）。"
                else:
                    logging.warning(f"レビュー生成APIからの応答が不正または空です: {response}")
                    return "レビューの生成に失敗しました（APIからの応答が不正または空です）。"
        except Exception as e:
            logging.error(f"レビュー生成中にエラーが発生しました: {e}", exc_info=True)
            return f"レビューの生成中にエラーが発生しました: {type(e).__name__}"

    # --- _save_chat_log_to_obsidian ---
    async def _save_chat_log_to_obsidian(self, user: discord.User, history: list, review: str):
        if not self.dbx or not self.dropbox_vault_path:
             logging.warning("Obsidianへのログ保存をスキップ: DropboxクライアントまたはVaultパスが未設定です。"); return

        now = datetime.now(JST); date_str = now.strftime('%Y-%m-%d'); timestamp = now.strftime('%Y%m%d%H%M%S')
        title = f"英会話ログ {user.display_name} {date_str}"
        safe_title_part = re.sub(r'[\\/*?:"<>|]', '_', f"{user.display_name}_{date_str}")
        filename = f"{timestamp}-英会話ログ_{safe_title_part}.md"

        log_parts = []
        for t in history:
            role = getattr(t, 'role', 'unknown')
            parts = getattr(t, 'parts', [])
            text_content = " ".join(getattr(p, 'text', '') for p in parts)
            if role in ['user', 'model'] and text_content:
                log_parts.append(f"- **{'You' if role == 'user' else 'AI'}:** {text_content}")
        conversation_log = "\n".join(log_parts)

        note_content = (f"# {title}\n\n- Date: {date_str}\n- Participant: {user.display_name}\n\n[[{date_str}]]\n\n"
                        f"---\n\n## 💬 Session Review\n{review}\n\n---\n\n## 📜 Full Transcript\n{conversation_log}\n")
        note_path = f"{self.dropbox_vault_path}{ENGLISH_LOG_PATH}/{filename}"

        try:
            await asyncio.to_thread(self.dbx.files_upload, note_content.encode('utf-8'), note_path, mode=WriteMode('add'))
            logging.info(f"英会話ログ保存成功: {note_path}")

            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"; daily_note_content = ""
            try:
                metadata, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                daily_note_content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    daily_note_content = f"# {date_str}\n"; logging.info(f"デイリーノートが見つからないため新規作成: {daily_note_path}")
                else: raise

            note_filename_for_link = filename.replace('.md', ''); link_path_part = ENGLISH_LOG_PATH.lstrip('/')
            # Create link text using only filename without timestamp if desired
            link_display_name = f"英会話ログ_{safe_title_part}" # Example without timestamp
            link_to_add = f"- [[{link_path_part}/{note_filename_for_link}|{link_display_name}]]" # Use display name

            new_daily_content = update_section(daily_note_content, link_to_add, DAILY_NOTE_ENGLISH_LOG_HEADER)
            await asyncio.to_thread(self.dbx.files_upload, new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
            logging.info(f"デイリーノート ({daily_note_path}) に英会話ログリンク追記成功。")

        except AuthError as e: logging.error(f"英会話ログ保存/デイリーノート更新 Dropbox認証エラー: {e}")
        except ApiError as e: logging.error(f"英会話ログ保存/デイリーノート更新 Dropbox APIエラー: {e}", exc_info=True)
        except Exception as e: logging.error(f"英会話ログ保存/デイリーノート更新 予期せぬエラー: {e}", exc_info=True)

    # --- _save_sakubun_log_to_obsidian ---
    async def _save_sakubun_log_to_obsidian(self, japanese_question: str, user_answer: str, feedback_text: str):
        if not self.dbx or not self.dropbox_vault_path:
             logging.warning("瞬間英作文ログのObsidian保存をスキップ: DropboxクライアントまたはVaultパスが未設定です。"); return

        now = datetime.now(JST); date_str = now.strftime('%Y-%m-%d'); timestamp = now.strftime('%Y%m%d%H%M%S')
        safe_title_part = re.sub(r'[\\/*?:"<>|]', '_', japanese_question[:20]); filename = f"{timestamp}-Sakubun_{safe_title_part}.md"
        # Adjusted regex to better find Model Answer section
        model_answers_match = re.search(r"^\#+\s*Model Answer(?:s)?\n+((?:^\s*[-*+].*(?:\n|$))+)", feedback_text, re.DOTALL | re.MULTILINE | re.IGNORECASE)
        model_answers = ""
        if model_answers_match:
            # Extract bullet points more reliably
            raw_answers = re.findall(r"^\s*[-*+]\s+(.+)", model_answers_match.group(1), re.MULTILINE)
            model_answers = "\n".join([f"- {ans.strip()}" for ans in raw_answers if ans.strip()])

        note_content = (f"# {date_str} 瞬間英作文\n\n- Date: [[{date_str}]]\n---\n\n## 問題\n{japanese_question}\n\n"
                        f"## あなたの回答\n{user_answer}\n\n## AIによるフィードバック\n{feedback_text}\n")
        if model_answers: note_content += f"---\n\n## モデルアンサー\n{model_answers}\n" # Add model answers if found
        note_path = f"{self.dropbox_vault_path}{SAKUBUN_LOG_PATH}/{filename}"

        try:
            await asyncio.to_thread(self.dbx.files_upload, note_content.encode('utf-8'), note_path, mode=WriteMode('add'))
            logging.info(f"瞬間英作文ログ保存成功: {note_path}")

            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"; daily_note_content = ""
            try:
                metadata, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                daily_note_content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    daily_note_content = f"# {date_str}\n"; logging.info(f"デイリーノートが見つからないため新規作成: {daily_note_path}")
                else: raise

            note_filename_for_link = filename.replace('.md', ''); link_path_part = SAKUBUN_LOG_PATH.lstrip('/')
            link_to_add = f"- [[{link_path_part}/{note_filename_for_link}|{japanese_question[:30]}...]]"
            new_daily_content = update_section(daily_note_content, link_to_add, DAILY_NOTE_SAKUBUN_LOG_HEADER)
            await asyncio.to_thread(self.dbx.files_upload, new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
            logging.info(f"デイリーノート ({daily_note_path}) に瞬間英作文ログリンク追記成功。")

        except AuthError as e: logging.error(f"瞬間英作文ログ保存/デイリーノート更新 Dropbox認証エラー: {e}")
        except ApiError as e: logging.error(f"瞬間英作文ログ保存/デイリーノート更新 Dropbox APIエラー: {e}", exc_info=True)
        except Exception as e: logging.error(f"瞬間英作文ログ保存/デイリーノート更新 予期せぬエラー: {e}", exc_info=True)


    # --- end_chat Command ---
    @app_commands.command(name="end", description="英会話を終了します")
    async def end_chat(self, interaction: discord.Interaction):
        if not self.is_ready:
             await interaction.response.send_message("英会話機能は現在利用できません（設定確認中）。", ephemeral=True); return
        if interaction.channel_id != self.channel_id:
             await interaction.response.send_message(f"このコマンドは英会話チャンネル (<#{self.channel_id}>) でのみ利用できます。", ephemeral=True); return

        user_id = interaction.user.id
        session_path = self._get_session_path(user_id)
        chat_session = self.chat_sessions.pop(user_id, None)

        if not chat_session:
             await interaction.response.send_message("アクティブなセッションが見つかりませんでした。", ephemeral=True); return

        await interaction.response.defer() # Defer before long operations

        review_text = "レビューの生成に失敗しました。"
        history_to_save = []
        important_phrases = []

        if hasattr(chat_session, 'history'):
            history_to_save = chat_session.history
            try:
                logging.info(f"Generating review for user {user_id}...")
                review_text = await self._generate_chat_review(history_to_save)
                logging.info(f"Review generated for user {user_id}.")
                important_phrases = extract_phrases_from_markdown_list(review_text, "重要フレーズ")

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

                # Save to Obsidian only if Dropbox is available
                if self.dbx:
                    await self._save_chat_log_to_obsidian(interaction.user, history_to_save, review_text)
                else:
                    logging.warning(f"Dropbox not available, skipping Obsidian log save for user {user_id}.")

            except Exception as e:
                 logging.error(f"Error saving session/generating review for user {user_id} on end: {e}", exc_info=True)
                 # Notify user about the error, using followup since deferred
                 try: await interaction.followup.send("セッション履歴の保存またはレビュー生成中にエラーが発生しました。", ephemeral=True)
                 except discord.HTTPException: pass # Ignore if followup fails

        # Display review in Discord
        review_embed = discord.Embed(
            title="💬 Conversation Review",
            description=review_text[:4000], # Limit description length
            color=discord.Color.gold(),
            timestamp=datetime.now(JST)
        ).set_footer(text=f"{interaction.user.display_name}'s session")

        # Create TTS View only if phrases exist and client is available
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
             try:
                 # Fallback: Send text, conditionally include view
                 fallback_kwargs = {"view": view} if view else {}
                 await interaction.followup.send(f"**Conversation Review:**\n{review_text[:1900]}", **fallback_kwargs)
             except discord.HTTPException as e2:
                 logging.error(f"Failed to send fallback review text: {e2}")
                 await interaction.followup.send("レビューの表示に失敗しました。ログを確認してください。", ephemeral=True)

        # Delete the session file from Dropbox
        if self.dbx:
            try:
                logging.info(f"Attempting to delete session file: {session_path}")
                await asyncio.to_thread(self.dbx.files_delete_v2, session_path)
                logging.info(f"Successfully deleted session file: {session_path}")
            except AuthError as e:
                 logging.error(f"Dropbox AuthError deleting session ({session_path}): {e}")
                 await interaction.followup.send("Dropbox認証エラーのため、セッションファイルの削除に失敗しました。", ephemeral=True)
            except ApiError as e:
                if isinstance(e.error, dropbox.exceptions.PathLookupError) and e.error.is_not_found():
                     logging.warning(f"Session file not found during deletion: {session_path}")
                else:
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
        if (not self.is_ready or
                message.author.bot or
                message.channel.id != self.channel_id or
                message.content.startswith('/')):
             return

        user_id = message.author.id

        # --- Handle Sakubun Answer ---
        if message.reference and message.reference.message_id:
            try:
                original_msg = await message.channel.fetch_message(message.reference.message_id)
                # Check if it's a reply to the bot's Sakubun question embed
                if (original_msg.author.id == self.bot.user.id and
                        original_msg.embeds and
                        "問" in original_msg.embeds[0].title and # More specific check
                        "瞬間英作文" in original_msg.embeds[0].title): # Check title too
                    await self.handle_sakubun_answer(message, message.content.strip(), original_msg)
                    return # Don't process as regular chat message
            except discord.NotFound:
                logging.warning(f"Original message for Sakubun reply not found: {message.reference.message_id}")
            except Exception as e_ref:
                logging.error(f"Error processing potential Sakubun reply reference: {e_ref}")

        # --- Handle Regular Chat Message ---
        if user_id in self.chat_sessions:
            chat = self.chat_sessions[user_id]
            async with message.channel.typing():
                try:
                    logging.info(f"Sending message to Gemini for user {user_id}")
                    response = await chat.send_message_async(message.content)
                    response_text = "Sorry, I couldn't generate a response."
                    if response and hasattr(response, 'text') and response.text:
                         response_text = response.text
                    elif response and hasattr(response, 'candidates') and response.candidates:
                         candidate = response.candidates[0]
                         if hasattr(candidate, 'finish_reason') and candidate.finish_reason != 'STOP':
                             reason = candidate.finish_reason
                             safety = getattr(candidate, 'safety_ratings', [])
                             logging.warning(f"Gemini response blocked. Reason: {reason}, Safety: {safety}")
                             response_text = f"(Response blocked due to: {reason})"
                         else: logging.warning(f"Gemini response has no text but finish reason is STOP: {response}")

                    logging.info(f"Received response from Gemini for user {user_id}")
                    view = TTSView(response_text, self.openai_client) if self.openai_client else None
                    await message.reply(f"**AI:** {response_text}", view=view)

                    # Save session after successful interaction
                    await self._save_session_to_dropbox(user_id, chat.history)

                except Exception as e:
                    logging.error(f"英会話中のメッセージ処理エラー for user {user_id}: {e}", exc_info=True)
                    await message.reply("Sorry, an error occurred while processing your message.")
        # else: # User sent a message but no active session - ignore or send hint?
            # pass


    # --- handle_sakubun_answer ---
    async def handle_sakubun_answer(self, message: discord.Message, user_answer: str, original_msg: discord.Message):
        if not self.is_ready: await message.reply("機能準備中です。"); return
        if not user_answer: await message.add_reaction("❓"); await asyncio.sleep(5); try: await message.remove_reaction("❓", self.bot.user) except discord.HTTPException: pass; return

        await message.add_reaction("🤔")
        japanese_question = original_msg.embeds[0].description.strip().replace("*","")
        prompt = f"""あなたはプロ英語教師です。添削と解説をしてください。
# 指示
- 評価してください。
- `### Model Answer` 見出しの下に**モデルアンサー英文のみを箇条書き (`- Answer Sentence`)** で2〜3個提示。
- 文法ポイント解説。
- Markdown形式で。
# 日本語の原文
{japanese_question}
# 学習者の英訳
{user_answer}"""
        feedback_text = "フィードバック生成失敗。"
        view = None
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            if response and hasattr(response, 'text') and response.text: feedback_text = response.text
            else: logging.warning(f"Sakubun feedback response invalid: {response}")

            feedback_embed = discord.Embed(title=f"添削結果: 「{japanese_question}」", description=feedback_text, color=discord.Color.green())
            # Use the helper function to extract model answers
            model_answers = extract_phrases_from_markdown_list(feedback_text, "Model Answer")
            if model_answers and self.openai_client: view = TTSView(model_answers, self.openai_client)

            await message.reply(embed=feedback_embed, view=view)
            await self._save_sakubun_log_to_obsidian(japanese_question, user_answer, feedback_text) # Save log

        except Exception as e_fb:
            logging.error(f"瞬間英作文フィードバック/保存エラー: {e_fb}", exc_info=True)
            await message.reply("フィードバック処理中にエラーが発生しました。")
        finally: # Correct indentation
             try:
                 await message.remove_reaction("🤔", self.bot.user)
             except discord.HTTPException:
                 pass


# --- setup Function ---
async def setup(bot):
    openai_key = os.getenv("OPENAI_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")
    dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
    dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
    dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
    channel_id = os.getenv("ENGLISH_LEARNING_CHANNEL_ID")

    # Check required environment variables
    if not all([gemini_key, dropbox_refresh_token, dropbox_app_key, dropbox_app_secret, channel_id]):
        logging.error("EnglishLearningCog: 必須の環境変数 (GEMINI_API_KEY, DROPBOX_REFRESH_TOKEN, DROPBOX_APP_KEY, DROPBOX_APP_SECRET, ENGLISH_LEARNING_CHANNEL_ID) が不足しているため、Cogをロードしません。")
        return

    try:
        channel_id_int = int(channel_id) # Ensure channel ID is int
    except ValueError:
        logging.error("EnglishLearningCog: ENGLISH_LEARNING_CHANNEL_ID must be a valid integer.")
        return

    # Pass credentials to the Cog's __init__
    cog_instance = EnglishLearningCog(
        bot,
        openai_key, # Can be None
        gemini_key,
        dropbox_refresh_token,
        dropbox_app_key,
        dropbox_app_secret
    )
    # Only add cog if it initialized successfully (is_ready is True)
    if cog_instance.is_ready:
        await bot.add_cog(cog_instance)
        logging.info("EnglishLearningCog loaded successfully.")
    else:
        logging.error("EnglishLearningCog failed to initialize and was not loaded.")