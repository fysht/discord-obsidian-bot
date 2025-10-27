import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import aiohttp
import openai
import google.generativeai as genai
from datetime import datetime, time
import zoneinfo
from pathlib import Path
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import re
import json
import asyncio
from PIL import Image
import io

# 共通関数をインポート
from utils.obsidian_utils import update_section
# Google Docs Handlerをインポート (エラーハンドリング付き)
try:
    from google_docs_handler import append_text_to_doc_async
    google_docs_enabled = True
    logging.info("Google Docs連携が有効です (ZeroSecondThinkingCog)。")
except ImportError:
    logging.warning("google_docs_handler.pyが見つからないため、Google Docs連携は無効です (ZeroSecondThinkingCog)。")
    google_docs_enabled = False
    # ダミー関数を定義
    async def append_text_to_doc_async(*args, **kwargs):
        logging.warning("Google Docs handler is not available.")
        pass

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
SUPPORTED_AUDIO_TYPES = [
    'audio/mpeg', 'audio/x-m4a', 'audio/ogg', 'audio/wav', 'audio/webm'
]
SUPPORTED_IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp'] # HEIC対応を追加する場合はここに'image/heic', 'image/heif'を追加

# 処理中・完了を示す絵文字
PROCESS_START_EMOJI = '⏳'
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'

# --- HEIC support (Optional: If pillow-heif is installed) ---
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    SUPPORTED_IMAGE_TYPES.append('image/heic')
    SUPPORTED_IMAGE_TYPES.append('image/heif')
    logging.info("HEIC/HEIF image support enabled.")
except ImportError:
    logging.warning("pillow_heif not installed. HEIC/HEIF support is disabled.")
# --- End HEIC support ---

# ==============================================================================
# === UI Components for Editing ================================================
# ==============================================================================

# --- Modal for Editing Text (Used for both Audio and Image) ---
class TextEditModal(discord.ui.Modal, title="テキストの編集"):
    memo_text = discord.ui.TextInput(
        label="認識されたテキスト（編集してください）",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1500 # Discord Modals have limits
    )

    def __init__(self, cog, original_question: str, initial_text: str, user_reply_message: discord.Message, bot_confirm_message: discord.Message, input_type_suffix: str):
        super().__init__(timeout=1800) # 30分
        self.cog = cog
        self.original_question = original_question
        self.memo_text.default = initial_text # Pre-fill
        self.user_reply_message = user_reply_message
        self.bot_confirm_message = bot_confirm_message
        self.input_type_suffix = input_type_suffix # e.g., "(edited audio)" or "(edited image)"

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        edited_text = self.memo_text.value
        logging.info(f"Memo edited and submitted by {interaction.user} (Type: {self.input_type_suffix}).")
        try:
            # Call the saving/processing function with the edited text
            await self.cog._save_and_continue_thinking(
                interaction,
                self.original_question,
                edited_text,
                self.user_reply_message, # Pass user's reply message
                f"{self.input_type_suffix}" # Pass the specific edited type
            )
            # --- Cleanup intermediate messages ---
            try:
                await self.bot_confirm_message.delete()
                logging.info(f"Deleted bot confirmation message {self.bot_confirm_message.id}")
            except discord.HTTPException as e_del_bot:
                logging.warning(f"Failed to delete bot confirmation message {self.bot_confirm_message.id}: {e_del_bot}")
            # --- End cleanup ---
            await interaction.followup.send("✅ 編集されたメモを処理しました。", ephemeral=True, delete_after=10)

        except Exception as e:
            logging.error(f"Error processing edited memo (Type: {self.input_type_suffix}): {e}", exc_info=True)
            await interaction.followup.send(f"❌ 編集されたメモの処理中にエラーが発生しました: {e}", ephemeral=True)
            try: await self.user_reply_message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logging.error(f"Error in TextEditModal: {error}", exc_info=True)
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ モーダルの処理中にエラーが発生しました: {error}", ephemeral=True)
        else:
             try:
                 await interaction.response.send_message(f"❌ モーダルの処理中にエラーが発生しました: {error}", ephemeral=True)
             except discord.InteractionResponded:
                  await interaction.followup.send(f"❌ モーダルの処理中にエラーが発生しました: {error}", ephemeral=True)
        try: await self.user_reply_message.add_reaction(PROCESS_ERROR_EMOJI)
        except discord.HTTPException: pass

# --- View for Confirming/Editing Text (Used for both Audio and Image) ---
class ConfirmTextView(discord.ui.View):
    def __init__(self, cog, original_question: str, recognized_text: str, user_reply_message: discord.Message, input_type_raw: str):
        super().__init__(timeout=3600) # 1時間
        self.cog = cog
        self.original_question = original_question
        self.recognized_text = recognized_text
        self.user_reply_message = user_reply_message
        self.input_type_raw = input_type_raw # "audio" or "image"
        self.bot_confirm_message = None # Will be set after sending the view

    @discord.ui.button(label="このまま投稿", style=discord.ButtonStyle.success, custom_id="confirm_text")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        logging.info(f"Confirm button clicked by {interaction.user} (Type: {self.input_type_raw})")
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            # Call saving function with the original recognized text
            await self.cog._save_and_continue_thinking(
                interaction,
                self.original_question,
                self.recognized_text,
                self.user_reply_message,
                f"{self.input_type_raw} (confirmed)"
            )
            # --- Cleanup intermediate messages ---
            try:
                await self.bot_confirm_message.delete()
                logging.info(f"Deleted bot confirmation message {self.bot_confirm_message.id}")
            except discord.HTTPException as e_del_bot:
                logging.warning(f"Failed to delete bot confirmation message {self.bot_confirm_message.id}: {e_del_bot}")
            # --- End cleanup ---
            await interaction.followup.send("✅ メモを処理しました。", ephemeral=True, delete_after=10)

        except Exception as e:
            logging.error(f"Error processing confirmed memo (Type: {self.input_type_raw}): {e}", exc_info=True)
            await interaction.followup.send(f"❌ 確認されたメモの処理中にエラーが発生しました: {e}", ephemeral=True)
            try: await self.user_reply_message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass
        finally:
             self.stop()

    @discord.ui.button(label="編集する", style=discord.ButtonStyle.primary, custom_id="edit_text")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        logging.info(f"Edit button clicked by {interaction.user} (Type: {self.input_type_raw})")
        # Open the Modal for editing
        modal = TextEditModal(
            self.cog,
            self.original_question,
            self.recognized_text,
            self.user_reply_message,
            self.bot_confirm_message,
            f"{self.input_type_raw} (edited)" # Pass suffix for modal
        )
        await interaction.response.send_modal(modal)
        self.stop()

    async def on_timeout(self):
        logging.info(f"ConfirmTextView timed out (Type: {self.input_type_raw}).")
        if self.bot_confirm_message:
            try:
                await self.bot_confirm_message.edit(content="確認・編集時間がタイムアウトしました。", view=None)
                await self.user_reply_message.add_reaction("⚠️")
            except discord.HTTPException as e:
                logging.warning(f"Failed to edit message on ConfirmTextView timeout: {e}")


# ==============================================================================
# === ZeroSecondThinkingCog ====================================================
# ==============================================================================

class ZeroSecondThinkingCog(commands.Cog):
    """
    Discord上でゼロ秒思考を支援するためのCog
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # --- 環境変数 ---
        self.channel_id = int(os.getenv("ZERO_SECOND_THINKING_CHANNEL_ID", "0"))
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.history_path = f"{self.dropbox_vault_path}/.bot/zero_second_thinking_history.json"

        # --- 状態管理 ---
        self.active_questions = {} # {message_id: question_text}
        self.user_last_interaction = {} # {user_id: message_id}

        # --- 初期化 ---
        self.is_ready = False
        if not all([self.channel_id, self.openai_api_key, self.gemini_api_key, self.dropbox_refresh_token]):
            logging.warning("ZeroSecondThinkingCog: 必要な環境変数が不足しています。")
        else:
            try:
                self.session = aiohttp.ClientSession()
                self.openai_client = openai.AsyncOpenAI(api_key=self.openai_api_key)
                genai.configure(api_key=self.gemini_api_key)
                self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
                self.gemini_vision_model = genai.GenerativeModel("gemini-2.5-pro")
                self.dbx = dropbox.Dropbox(oauth2_refresh_token=self.dropbox_refresh_token, app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret)
                self.dbx.users_get_current_account()
                self.is_ready = True
                logging.info("✅ ZeroSecondThinkingCogが正常に初期化されました。")
            except Exception as e:
                 logging.error(f"ZeroSecondThinkingCogの初期化中にエラー: {e}", exc_info=True)


    async def cog_unload(self):
        """Cogのアンロード時にセッションを閉じる"""
        if self.is_ready:
            if hasattr(self, 'session') and self.session and not self.session.closed:
                await self.session.close()


    async def _get_thinking_history(self) -> list:
        """過去の思考履歴をDropboxから読み込む"""
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, self.history_path)
            data = json.loads(res.content.decode('utf-8'))
            return data if isinstance(data, list) else []
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                return []
            logging.error(f"思考履歴の読み込みに失敗: {e}")
            return []
        except json.JSONDecodeError:
            logging.error(f"思考履歴ファイル ({self.history_path}) のJSON形式が不正です。空のリストを返します。")
            return []
        except Exception as e:
            logging.error(f"思考履歴の読み込み中に予期せぬエラー: {e}", exc_info=True)
            return []


    async def _save_thinking_history(self, history: list):
        """思考履歴をDropboxに保存（最新10件まで）"""
        try:
            limited_history = history[-10:]
            await asyncio.to_thread(
                self.dbx.files_upload,
                json.dumps(limited_history, ensure_ascii=False, indent=2).encode('utf-8'),
                self.history_path,
                mode=WriteMode('overwrite')
            )
        except Exception as e:
            logging.error(f"思考履歴の保存に失敗: {e}", exc_info=True)


    @app_commands.command(name="zst_start", description="ゼロ秒思考の新しいお題を開始します。")
    async def zst_start(self, interaction: discord.Interaction):
        """Generates and posts a new thinking prompt."""
        if not self.is_ready:
            await interaction.response.send_message("ゼロ秒思考機能は現在準備中です。", ephemeral=True)
            return
        if interaction.channel_id != self.channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.channel_id}> でのみ使用できます。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False, thinking=True)

        try:
            history = await self._get_thinking_history()
            history_context = "\n".join([f"- {item.get('question', 'Q')}: {item.get('answer', 'A')[:100]}..." for item in history])

            prompt = f"""
            あなたは思考を深めるための問いを投げかけるコーチです。
            私が「ゼロ秒思考」を行うのを支援するため、質の高いお題を1つだけ生成してください。
            # 指示
            - ユーザーの過去の思考履歴を参考に、より深い洞察を促す問いを生成してください。
            - 過去の回答内容を掘り下げるような質問や、関連するが異なる視点からの質問が望ましいです。
            - 過去数回の質問と重複しないようにしてください。
            - お題はビジネス、自己啓発、人間関係、創造性など、多岐にわたるテーマから選んでください。
            - 前置きや挨拶は一切含めず、お題のテキストのみを生成してください。
            # 過去の思考履歴（質問と回答の要約）
            {history_context if history_context else "履歴はありません。"}
            ---
            お題:
            """
            response = await self.gemini_model.generate_content_async(prompt)
            question = "デフォルトのお題: 今、一番気になっていることは何ですか？"
            if response and hasattr(response, 'text') and response.text.strip():
                 question = response.text.strip().replace("*", "")
            else:
                 logging.warning(f"Geminiからの質問生成に失敗、または空の応答: {response}")

            embed = discord.Embed(title="🤔 ゼロ秒思考 - 新しいお題", description=f"お題: **{question}**", color=discord.Color.teal())
            embed.set_footer(text="このメッセージに返信する形で、思考を書き出してください（音声・手書きメモ画像も可）。")

            sent_message = await interaction.followup.send(embed=embed)

            self.active_questions[sent_message.id] = question
            logging.info(f"New thinking question posted via command: ID {sent_message.id}, Q: {question}")

        except Exception as e:
            logging.error(f"[Zero-Second Thinking] /zst_start コマンドエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ 質問の生成中にエラーが発生しました: {e}", ephemeral=True)


    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """メッセージ投稿を監視し、Zero-Second Thinkingのフローを処理する"""
        if not self.is_ready or message.author.bot or message.channel.id != self.channel_id:
            return

        if message.content.strip().startswith('/'):
             return

        if not message.reference or not message.reference.message_id:
            return

        original_message_id = message.reference.message_id

        if original_message_id not in self.active_questions:
             return

        self.user_last_interaction[message.author.id] = original_message_id
        channel = message.channel

        try:
            original_msg = await channel.fetch_message(original_message_id)
            if not original_msg.embeds: return
        except (discord.NotFound, discord.Forbidden):
             logging.warning(f"Could not fetch original question message {original_message_id} for verification.")
        except Exception as e_fetch:
             logging.error(f"Error fetching original message {original_message_id}: {e_fetch}", exc_info=True)
             return

        original_question_text = self.active_questions.get(original_message_id, "不明なお題")
        logging.info(f"Processing reply to question ID {original_message_id}: {original_question_text}")

        input_type = "text"
        attachment_to_process = None
        if message.attachments:
            img_attachment = next((att for att in message.attachments if att.content_type in SUPPORTED_IMAGE_TYPES), None)
            audio_attachment = next((att for att in message.attachments if att.content_type in SUPPORTED_AUDIO_TYPES), None)

            if img_attachment:
                input_type = "image"
                attachment_to_process = img_attachment
                logging.info(f"Image attachment detected for message {message.id}")
            elif audio_attachment:
                input_type = "audio"
                attachment_to_process = audio_attachment
                logging.info(f"Audio attachment detected for message {message.id}")

        if input_type == "text" and not message.content.strip():
             logging.info("Empty text reply detected. Ignoring.")
             try: await message.add_reaction("❓")
             except discord.HTTPException: pass
             return

        # Start processing
        await self._process_thinking_memo(message, original_question_text, original_message_id, input_type, attachment_to_process)

    async def _process_thinking_memo(self, user_reply_message: discord.Message, original_question: str, original_question_id: int, input_type: str, attachment: discord.Attachment = None):
        """Processes the user's thinking memo. Asks for edit confirmation for audio/image."""
        temp_audio_path = None
        formatted_answer = "回答の処理に失敗しました。"

        try:
            await user_reply_message.add_reaction(PROCESS_START_EMOJI)
        except discord.HTTPException: pass

        try:
            # >>>>>>>>>>>>>>>>>> MODIFICATION START <<<<<<<<<<<<<<<<<<
            if input_type == "audio" and attachment:
                logging.info("Processing audio memo...")
                # --- Audio Transcription ---
                temp_audio_path = Path(f"./temp_audio_{user_reply_message.id}_{attachment.filename}")
                async with self.session.get(attachment.url) as resp:
                    if resp.status == 200:
                        with open(temp_audio_path, 'wb') as f: f.write(await resp.read())
                    else: raise Exception(f"音声ファイルのダウンロード失敗: Status {resp.status}")
                with open(temp_audio_path, "rb") as audio_file:
                    transcription = await self.openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file)
                transcribed_text = transcription.text
                logging.info("Audio transcribed successfully.")
                # --- End Transcription ---

                # --- Formatting ---
                formatting_prompt = (
                    "以下の音声メモの文字起こしを、構造化された箇条書きのMarkdown形式でまとめてください。\n"
                    "箇条書きの本文のみを生成し、前置きや返答は一切含めないでください。\n\n"
                    f"---\n\n{transcribed_text}"
                )
                response = await self.gemini_model.generate_content_async(formatting_prompt)
                formatted_answer = response.text.strip() if response and hasattr(response, 'text') else transcribed_text
                logging.info("Audio memo formatted.")
                # --- End Formatting ---

                # --- Send for confirmation ---
                try: await user_reply_message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                except discord.HTTPException: pass

                confirm_view = ConfirmTextView(self, original_question, formatted_answer, user_reply_message, "audio")
                bot_confirm_msg = await user_reply_message.reply(
                    f"**🎤 認識された音声メモ:**\n```markdown\n{formatted_answer}\n```\n内容を確認し、問題なければ「このまま投稿」、修正する場合は「編集する」ボタンを押してください。",
                    view=confirm_view
                )
                confirm_view.bot_confirm_message = bot_confirm_msg
                logging.info(f"Sent recognized audio text and confirm/edit buttons for message {user_reply_message.id}")
                return # Wait for button interaction
                # --- End Confirmation ---

            elif input_type == "image" and attachment:
                logging.info("Processing image memo (handwritten)...")
                # --- Image Recognition ---
                async with self.session.get(attachment.url) as resp:
                    if resp.status != 200:
                        raise Exception(f"画像ファイルのダウンロードに失敗: Status {resp.status}")
                    image_bytes = await resp.read()
                try:
                    img = Image.open(io.BytesIO(image_bytes))
                except Exception as e_pil:
                     logging.error(f"Failed to open image using Pillow: {e_pil}", exc_info=True)
                     raise Exception("画像ファイルの形式が無効、または破損している可能性があります。")
                vision_prompt = [
                    "この画像は手書きのメモです。内容を読み取り、箇条書きのMarkdown形式でテキスト化してください。返答には前置きや説明は含めず、箇条書きのテキスト本体のみを生成してください。",
                    img,
                ]
                response = await self.gemini_vision_model.generate_content_async(vision_prompt)
                recognized_text = response.text.strip() if response and hasattr(response, 'text') else "手書きメモの読み取りに失敗しました。"
                logging.info("Image memo recognized by Gemini Vision.")
                # --- End Recognition ---

                # --- Send for confirmation ---
                try: await user_reply_message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                except discord.HTTPException: pass

                confirm_view = ConfirmTextView(self, original_question, recognized_text, user_reply_message, "image")
                bot_confirm_msg = await user_reply_message.reply(
                    f"**📝 認識された手書きメモ:**\n```markdown\n{recognized_text}\n```\n内容を確認し、問題なければ「このまま投稿」、修正する場合は「編集する」ボタンを押してください。",
                    view=confirm_view
                )
                confirm_view.bot_confirm_message = bot_confirm_msg
                logging.info(f"Sent recognized image text and confirm/edit buttons for message {user_reply_message.id}")
                return # Wait for button interaction
                # --- End Confirmation ---

            # >>>>>>>>>>>>>>>>>> MODIFICATION END <<<<<<<<<<<<<<<<<<

            else: # Text input
                logging.info("Processing text memo...")
                formatted_answer = user_reply_message.content.strip()
                 # Directly save and continue for text
                await self._save_and_continue_thinking(
                    user_reply_message, # Use user's message as context
                    original_question,
                    formatted_answer,
                    user_reply_message, # Pass user's message for context
                    input_type
                )

        except Exception as e:
            logging.error(f"[Zero-Second Thinking] Error in _process_thinking_memo: {e}", exc_info=True)
            try: await user_reply_message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except discord.HTTPException: pass
            try: await user_reply_message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass
            try: await user_reply_message.reply(f"❌ メモの処理中にエラーが発生しました: {e}")
            except discord.HTTPException: pass
        finally:
            if temp_audio_path and os.path.exists(temp_audio_path):
                try:
                    os.remove(temp_audio_path)
                    logging.info(f"Temporary audio file removed: {temp_audio_path}")
                except OSError as e_rm:
                     logging.error(f"一時音声ファイル削除失敗: {e_rm}")


    async def _save_and_continue_thinking(self, interaction_or_message, original_question: str, final_answer: str, context_message: discord.Message, input_type: str):
        """Saves the final answer, updates history, saves to Obsidian/GDocs, asks a follow-up, and manages reactions/cleanup."""
        original_question_id = context_message.reference.message_id if context_message.reference else None

        # Determine how to send the follow-up question
        async def send_followup_question(embed):
            # Always send to the channel of the context message
            channel = context_message.channel
            return await channel.send(embed=embed)

        try:
            # Add processing reaction if not already added
            if isinstance(context_message, discord.Message):
                 has_hourglass = any(r.emoji == PROCESS_START_EMOJI and r.me for r in context_message.reactions)
                 if not has_hourglass:
                      try: await context_message.add_reaction(PROCESS_START_EMOJI)
                      except discord.HTTPException: pass


            logging.info(f"Saving final answer for question: {original_question}")
            # --- Update History ---
            history = await self._get_thinking_history()
            history.append({"question": original_question, "answer": final_answer})
            await self._save_thinking_history(history)
            logging.info("Thinking history updated.")

            # --- Save to Obsidian ---
            now = datetime.now(JST)
            daily_note_date = now.strftime('%Y-%m-%d')
            safe_title = re.sub(r'[\\/*?:"<>|]', "", original_question)[:50]
            if not safe_title: safe_title = "Untitled"
            timestamp = now.strftime('%Y%m%d%H%M%S')
            note_filename = f"{timestamp}-{safe_title}.md"
            note_path = f"{self.dropbox_vault_path}/Zero-Second Thinking/{note_filename}"

            new_note_content = (
                f"# {original_question}\n\n"
                f"- **Source:** Discord ({input_type.capitalize()})\n"
                f"- **作成日:** {daily_note_date}\n\n"
                f"[[{daily_note_date}]]\n\n"
                f"---\n\n"
                f"## 回答\n{final_answer}"
            )
            await asyncio.to_thread(self.dbx.files_upload, new_note_content.encode('utf-8'), note_path, mode=WriteMode('add'))
            logging.info(f"[Zero-Second Thinking] 新規ノートを作成: {note_path}")

            # --- Add link to Daily Note ---
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"
            daily_note_content = ""
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                daily_note_content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    daily_note_content = f"# {daily_note_date}\n"
                    logging.info(f"デイリーノートが見つからなかったため新規作成: {daily_note_path}")
                else: raise

            note_filename_for_link = note_filename.replace('.md', '')
            link_to_add = f"- [[Zero-Second Thinking/{note_filename_for_link}|{original_question}]]"
            section_header = "## Zero-Second Thinking"
            new_daily_content = update_section(daily_note_content, link_to_add, section_header)
            await asyncio.to_thread(self.dbx.files_upload, new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))
            logging.info(f"デイリーノートにリンクを追記: {daily_note_path}")

            # --- Save to Google Docs ---
            if google_docs_enabled:
                gdoc_content = f"## 質問\n{original_question}\n\n## 回答\n{final_answer}"
                gdoc_title = f"ゼロ秒思考 - {daily_note_date} - {original_question[:30]}"
                try:
                    await append_text_to_doc_async(
                        text_to_append=gdoc_content,
                        source_type="Zero-Second Thinking",
                        title=gdoc_title
                    )
                    logging.info("Google Docsにゼロ秒思考ログを保存しました。")
                except Exception as e_gdoc:
                    logging.error(f"Google Docsへのゼロ秒思考ログ保存中にエラー: {e_gdoc}", exc_info=True)


            # --- Add completion reaction ---
            if isinstance(context_message, discord.Message):
                 try:
                     await context_message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                     await context_message.add_reaction(PROCESS_COMPLETE_EMOJI)
                 except discord.HTTPException: pass

            # --- Remove original question from active list ---
            if original_question_id:
                self.active_questions.pop(original_question_id, None)
                logging.info(f"Question ID {original_question_id} removed from active list.")


            # --- Ask Follow-up Question ---
            digging_prompt = f"""
            ユーザーは「ゼロ秒思考」を行っています。以下の「元の質問」と「ユーザーの回答」を踏まえて、思考をさらに深めるための鋭い掘り下げ質問を1つだけ生成してください。
            # 元の質問
            {original_question}
            # ユーザーの回答
            {final_answer}
            ---
            掘り下げ質問 (質問文のみ):
            """
            response = await self.gemini_model.generate_content_async(digging_prompt)
            new_question = "追加の質問: さらに詳しく教えてください。" # Fallback
            if response and hasattr(response, 'text') and response.text.strip():
                 potential_question = response.text.strip().replace("*", "")
                 if "?" in potential_question and len(potential_question.split('\n')) == 1:
                      new_question = potential_question
                 else:
                      logging.warning(f"Unexpected format for follow-up question: {potential_question}. Using fallback.")
            else:
                 logging.warning(f"Geminiからの深掘り質問生成に失敗、または空の応答: {response}")

            embed = discord.Embed(title="🤔 さらに深掘りしましょう", description=f"お題: **{new_question}**", color=discord.Color.blue())
            embed.set_footer(text="このメッセージに返信する形で、思考を書き出してください。`/zst_end`で終了。")

            sent_message = await send_followup_question(embed=embed)

            # Store the new follow-up question
            self.active_questions[sent_message.id] = new_question
            logging.info(f"Follow-up question posted: ID {sent_message.id}, Q: {new_question}")

            # >>>>>>>>>>>>>>>>>> MODIFICATION START (Delete original user reply) <<<<<<<<<<<<<<<<<<
            # --- Delete original user reply message (text, audio, image) ---
            if isinstance(context_message, discord.Message):
                 try:
                     await context_message.delete()
                     logging.info(f"Deleted original user reply message {context_message.id}")
                 except discord.HTTPException as e_del_user:
                     logging.warning(f"Failed to delete user reply message {context_message.id}: {e_del_user}")
            # >>>>>>>>>>>>>>>>>> MODIFICATION END <<<<<<<<<<<<<<<<<<


        except Exception as e_save:
            logging.error(f"[Save/Continue Error] Error saving memo or asking follow-up: {e_save}", exc_info=True)
            error_message_content = f"❌ メモの保存または次の質問の生成中にエラーが発生しました: {e_save}"
            if isinstance(interaction_or_message, discord.Interaction):
                 if interaction_or_message.response.is_done(): await interaction_or_message.followup.send(error_message_content, ephemeral=True)
                 else:
                      try: await interaction_or_message.response.send_message(error_message_content, ephemeral=True)
                      except discord.InteractionResponded: await interaction_or_message.followup.send(error_message_content, ephemeral=True)
            elif isinstance(context_message, discord.Message):
                 await context_message.reply(error_message_content)

            if isinstance(context_message, discord.Message):
                 try: await context_message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                 except discord.HTTPException: pass
                 try: await context_message.add_reaction(PROCESS_ERROR_EMOJI)
                 except discord.HTTPException: pass
            if original_question_id:
                self.active_questions.pop(original_question_id, None)


    @app_commands.command(name="zst_end", description="現在のゼロ秒思考セッション（ユーザーごと）を終了します。")
    async def zst_end(self, interaction: discord.Interaction):
        """Ends the user's current thinking thread."""
        if not self.is_ready:
            await interaction.response.send_message("ゼロ秒思考機能は現在準備中です。", ephemeral=True)
            return
        if interaction.channel_id != self.channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.channel_id}> でのみ使用できます。", ephemeral=True)
            return

        user_id = interaction.user.id
        last_interacted_question_id = self.user_last_interaction.pop(user_id, None)

        # Remove any active questions associated with the last interaction (if applicable)
        if last_interacted_question_id:
             self.active_questions.pop(last_interacted_question_id, None)
             logging.info(f"User {user_id} ended their ZST session. Cleared state for question {last_interacted_question_id}.")
        else:
             logging.info(f"User {user_id} used /zst_end but had no active interaction tracked.")

        # Also clear any questions potentially asked TO this user if needed?
        # For now, just clearing the user's interaction state seems sufficient.

        await interaction.response.send_message("ゼロ秒思考セッションを終了しました。新しいお題は `/zst_start` で始められます。", ephemeral=True, delete_after=15)


async def setup(bot: commands.Bot):
    """CogをBotに追加する"""
    if not all([os.getenv("ZERO_SECOND_THINKING_CHANNEL_ID"),
                os.getenv("OPENAI_API_KEY"),
                os.getenv("GEMINI_API_KEY"),
                os.getenv("DROPBOX_REFRESH_TOKEN"),
                os.getenv("DROPBOX_APP_KEY"),
                os.getenv("DROPBOX_APP_SECRET")]):
        logging.error("ZeroSecondThinkingCog: 必要な環境変数が不足しているため、Cogをロードしません。")
        return
    try:
         from PIL import Image # Pillow の存在確認
    except ImportError:
         logging.error("ZeroSecondThinkingCog: Pillowライブラリが見つかりません。手書きメモ機能を使用するには `pip install Pillow` を実行してください。Cogをロードしません。")
         return

    cog_instance = ZeroSecondThinkingCog(bot)
    if cog_instance.is_ready:
        await bot.add_cog(cog_instance)
        logging.info("ZeroSecondThinkingCog loaded successfully.")
    else:
        logging.error("ZeroSecondThinkingCog failed to initialize properly and was not loaded.")