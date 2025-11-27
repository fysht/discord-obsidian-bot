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
    logging.info("utils/obsidian_utils.pyを読み込みました。")
except ImportError:
    logging.warning("utils/obsidian_utils.pyが見つかりません。")
    # Define a dummy function if import fails
    def update_section(current_content: str, link_to_add: str, section_header: str) -> str:
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


# --- Cog: EnglishLearningCog ---
class EnglishLearningCog(commands.Cog, name="EnglishLearning"):
    """瞬間英作文とAI壁打ちチャットによる英語学習を支援するCog"""

    # --- __init__ ---
    def __init__(self, bot: commands.Bot, gemini_api_key, dropbox_refresh_token, dropbox_app_key, dropbox_app_secret):
        self.bot = bot
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
                self.is_ready = False
            except Exception as e:
                logging.error(f"Failed to initialize Dropbox client for EnglishLearningCog: {e}", exc_info=True)
                self.is_ready = False
        else:
            logging.warning("Dropbox credentials missing. Session saving/loading will be disabled.")
            self.is_ready = False # Dropbox is required for persistence

        # Check other requirements and update readiness
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
        if channel:
            await self._run_sakubun_session(channel, 2, "朝")
        else: logging.error(f"Sakubun channel not found: {self.channel_id}")

    @tasks.loop(time=EVENING_SAKUBUN_TIME)
    async def evening_sakubun_task(self):
        channel = self.bot.get_channel(self.channel_id)
        if channel:
            await self._run_sakubun_session(channel, 2, "夜")
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
        session = await self._load_session_from_dropbox(user_id)

        system_instruction = """
        あなたはフレンドリーな英会話パートナーです。気軽なチャット相手として、ユーザーと短いメッセージで会話のキャッチボールをしてください。

        # あなたの役割
        1.  **短い応答:** 1〜2文程度の短い返答や質問を心がけてください。長文の解説は不要です。
        2.  **会話の継続:** ユーザーの発言に共感したり、簡単な質問を返したりして、会話が続くようにしてください。例: "Oh really?", "That sounds interesting!", "What happened next?", "How was it?"
        3.  **自然な訂正:** もしユーザーの英語に明らかな誤りや不自然な点があれば、会話の流れの中でさりげなく修正してください。例: User: "I go park yesterday." -> AI: "Oh, you went to the park yesterday! Cool. Did you have fun?"
        4.  **常に英語:** あなたの返答は常に自然な英語で行ってください。
        """
        model_with_instruction = genai.GenerativeModel("gemini-2.5-pro", system_instruction=system_instruction)

        chat_session = None
        response_text = ""

        try:
            # Resume session if history exists
            if session is not None:
                logging.info(f"セッション再開: {session_path}")
                chat_session = model_with_instruction.start_chat(history=session)
                # Send a light resume message
                resume_prompt = "Hey there! Let's pick up where we left off. What's up?"
                response = await asyncio.wait_for(chat_session.send_message_async(resume_prompt), timeout=60)
                response_text = response.text if response and hasattr(response, "text") else "Hi again! What's new?"
            # Start new session if no history
            else:
                logging.info(f"新規セッション開始: {session_path}")
                chat_session = model_with_instruction.start_chat(history=[])
                # Send a light initial greeting
                initial_prompt = "Hey! Ready to chat in English? How's your day going?"
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

        await interaction.followup.send(f"**AI:** {response_text}")

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
2.  **重要例文**: 今回の会話で使われた、または学ぶべき重要な英単語やフレーズを3〜5個選び、**それぞれについて自然な英語の例文を作成してください**。**必ず `### 重要例文` という見出しの下に、例文のみを箇条書き (`- Example sentence.`) で記述してください。**
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
            link_display_name = f"英会話ログ ({user.display_name})"
            link_to_add = f"- [[{link_path_part}/{note_filename_for_link}|{link_display_name}]]"

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

        model_answers_match = re.search(r"^\#+\s*Model Answer(?:s)?\s*?\n+((?:^\s*[-*+].*(?:\n|$))+)", feedback_text, re.DOTALL | re.MULTILINE | re.IGNORECASE)
        model_answers = ""
        if model_answers_match:
            raw_answers = re.findall(r"^\s*[-*+]\s+(.+)", model_answers_match.group(1), re.MULTILINE)
            model_answers = "\n".join([f"- {ans.strip()}" for ans in raw_answers if ans.strip()])

        note_content = (f"# {date_str} 瞬間英作文\n\n- Date: [[{date_str}]]\n---\n\n## 問題\n{japanese_question}\n\n"
                        f"## あなたの回答\n{user_answer}\n\n## AIによるフィードバック\n{feedback_text}\n")
        if model_answers: note_content += f"---\n\n## モデルアンサー\n{model_answers}\n"
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

        await interaction.response.defer()

        review_text = "レビューの生成に失敗しました。"
        history_to_save = []

        if hasattr(chat_session, 'history'):
            history_to_save = chat_session.history
            try:
                logging.info(f"Generating review for user {user_id}...")
                review_text = await self._generate_chat_review(history_to_save)
                logging.info(f"Review generated for user {user_id}.")

                if self.dbx:
                    await self._save_chat_log_to_obsidian(interaction.user, history_to_save, review_text)
                else:
                    logging.warning(f"Dropbox not available, skipping Obsidian log save for user {user_id}.")

            except Exception as e:
                 logging.error(f"Error saving session/generating review for user {user_id} on end: {e}", exc_info=True)
                 try: await interaction.followup.send("セッション履歴の保存またはレビュー生成中にエラーが発生しました。", ephemeral=True)
                 except discord.HTTPException: pass

        review_embed = discord.Embed(
            title="💬 Conversation Review",
            description=review_text[:4000],
            color=discord.Color.gold(),
            timestamp=datetime.now(JST)
        ).set_footer(text=f"{interaction.user.display_name}'s session")

        await interaction.followup.send(embed=review_embed)

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
                logging.warning("セッション終了処理中にエラーが発生しました（ファイル削除）。")
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

        if message.reference and message.reference.message_id:
            try:
                original_msg = await message.channel.fetch_message(message.reference.message_id)
                if (original_msg.author.id == self.bot.user.id and
                        original_msg.embeds and
                        "問" in original_msg.embeds[0].title and
                        original_msg.embeds[0].footer and
                        original_msg.embeds[0].footer.text == "このメッセージに返信する形で英訳を投稿してください。"):
                    await self.handle_sakubun_answer(message, message.content.strip(), original_msg)
                    return
            except discord.NotFound:
                logging.warning(f"Original message for Sakubun reply not found: {message.reference.message_id}")
            except Exception as e_ref:
                logging.error(f"Error processing potential Sakubun reply reference: {e_ref}")

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
                         else:
                             logging.warning(f"Gemini response has no text but finish reason is STOP: {response}")
                    else:
                         logging.warning(f"Invalid response structure from Gemini: {response}")

                    logging.info(f"Received response from Gemini for user {user_id}")
                    # TTSView生成を削除
                    await message.reply(f"**AI:** {response_text}")

                    await self._save_session_to_dropbox(user_id, chat.history)

                except Exception as e:
                    logging.error(f"英会話中のメッセージ処理エラー for user {user_id}: {e}", exc_info=True)
                    await message.reply("Sorry, an error occurred while processing your message.")

    # --- handle_sakubun_answer ---
    async def handle_sakubun_answer(self, message: discord.Message, user_answer: str, original_msg: discord.Message):
        if not self.is_ready:
            await message.reply("機能準備中です。")
            return
        if not user_answer:
            await message.add_reaction("❓")
            await asyncio.sleep(5)
            try:
                await message.remove_reaction("❓", self.bot.user)
            except discord.HTTPException:
                logging.warning(f"リアクション❓の削除に失敗 (Message ID: {message.id})")
            return

        await message.add_reaction("🤔")
        japanese_question = original_msg.embeds[0].description.strip().replace("*","")

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

        feedback_text = "フィードバック生成失敗。"
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            if response and hasattr(response, 'text') and response.text: feedback_text = response.text
            else: logging.warning(f"Sakubun feedback response invalid: {response}")

            feedback_embed = discord.Embed(title=f"添削結果: 「{japanese_question}」", description=feedback_text[:4000], color=discord.Color.green())

            await message.reply(embed=feedback_embed)

            await self._save_sakubun_log_to_obsidian(japanese_question, user_answer, feedback_text)

        except Exception as e_fb:
            logging.error(f"瞬間英作文フィードバック/保存エラー: {e_fb}", exc_info=True)
            await message.reply("フィードバック処理中にエラーが発生しました。")
        finally:
             try:
                 await message.remove_reaction("🤔", self.bot.user)
             except discord.HTTPException:
                 pass


# --- setup Function ---
async def setup(bot):
    gemini_key = os.getenv("GEMINI_API_KEY")
    dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
    dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
    dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
    channel_id = os.getenv("ENGLISH_LEARNING_CHANNEL_ID")

    if not all([gemini_key, dropbox_refresh_token, dropbox_app_key, dropbox_app_secret, channel_id]):
        logging.error("EnglishLearningCog: 必須の環境変数 (GEMINI_API_KEY, DROPBOX_REFRESH_TOKEN, DROPBOX_APP_KEY, DROPBOX_APP_SECRET, ENGLISH_LEARNING_CHANNEL_ID) が不足しているため、Cogをロードしません。")
        return

    try:
        channel_id_int = int(channel_id)
    except ValueError:
        logging.error("EnglishLearningCog: ENGLISH_LEARNING_CHANNEL_ID must be a valid integer.")
        return

    cog_instance = EnglishLearningCog(
        bot,
        gemini_key,
        dropbox_refresh_token,
        dropbox_app_key,
        dropbox_app_secret
    )
    if cog_instance.is_ready:
        await bot.add_cog(cog_instance)
        logging.info("EnglishLearningCog loaded successfully.")
    else:
        logging.error("EnglishLearningCog failed to initialize and was not loaded.")