import os
import discord
from discord import app_commands # app_commands をインポート
from discord.ext import commands, tasks
import logging
import aiohttp
import openai
import google.generativeai as genai
from datetime import datetime, time
import zoneinfo # zoneinfo をインポート
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
THINKING_TIMES = [
    time(hour=9, minute=0, tzinfo=JST),
    time(hour=12, minute=0, tzinfo=JST),
    time(hour=15, minute=0, tzinfo=JST),
    time(hour=18, minute=0, tzinfo=JST),
    time(hour=21, minute=0, tzinfo=JST),
]

# 処理中を示す絵文字
PROCESS_START_EMOJI = '⏳'
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'

# --- HEIC support (Optional: If pillow-heif is installed) ---
try:
    # from PIL import Image # Already imported above
    import pillow_heif
    pillow_heif.register_heif_opener()
    # HEICのMIMEタイプをサポートリストに追加
    SUPPORTED_IMAGE_TYPES.append('image/heic')
    SUPPORTED_IMAGE_TYPES.append('image/heif')
    logging.info("HEIC/HEIF image support enabled.")
except ImportError:
    logging.warning("pillow_heif not installed. HEIC/HEIF support is disabled.")
# --- End HEIC support ---

# ==============================================================================
# === UI Components for Handwritten Memo Editing =============================
# ==============================================================================

class HandwrittenMemoEditModal(discord.ui.Modal, title="手書きメモの編集"):
    memo_text = discord.ui.TextInput(
        label="認識されたテキスト（編集してください）",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1500 # Discord Modals have limits
    )

    def __init__(self, cog, original_question: str, initial_text: str, original_reply_message: discord.Message):
        super().__init__(timeout=1800) # 30分
        self.cog = cog
        self.original_question = original_question
        self.memo_text.default = initial_text # Pre-fill with recognized text
        self.original_reply_message = original_reply_message # Keep track of the message the user replied to

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True) # Defer modal submission response
        edited_text = self.memo_text.value
        logging.info(f"Handwritten memo edited and submitted by {interaction.user}.")
        try:
            # Call the saving/processing function with the edited text and original context
            await self.cog._save_and_continue_thinking(
                interaction, # Pass interaction for followups
                self.original_question,
                edited_text,
                self.original_reply_message,
                input_type="image (edited)" # Indicate it was edited
            )
            await interaction.followup.send("✅ 編集された手書きメモを処理しました。", ephemeral=True)
        except Exception as e:
            logging.error(f"Error processing edited handwritten memo: {e}", exc_info=True)
            await interaction.followup.send(f"❌ 編集されたメモの処理中にエラーが発生しました: {e}", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logging.error(f"Error in HandwrittenMemoEditModal: {error}", exc_info=True)
        # Try sending an ephemeral follow-up message if the response is already done
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ モーダルの処理中にエラーが発生しました: {error}", ephemeral=True)
        else:
            # Otherwise, try sending an ephemeral response message
             try:
                 await interaction.response.send_message(f"❌ モーダルの処理中にエラーが発生しました: {error}", ephemeral=True)
             except discord.InteractionResponded: # If it was somehow responded to already
                  await interaction.followup.send(f"❌ モーダルの処理中にエラーが発生しました: {error}", ephemeral=True)


class EditHandwrittenView(discord.ui.View):
    def __init__(self, cog, original_question: str, recognized_text: str, original_reply_message: discord.Message):
        super().__init__(timeout=3600) # 1時間
        self.cog = cog
        self.original_question = original_question
        self.recognized_text = recognized_text
        self.original_reply_message = original_reply_message

        # Create the edit button
        edit_button = discord.ui.Button(label="編集する", style=discord.ButtonStyle.primary, custom_id="edit_handwritten_memo")
        edit_button.callback = self.edit_button_callback # Assign the callback
        self.add_item(edit_button)

    async def edit_button_callback(self, interaction: discord.Interaction):
        logging.info(f"Edit button clicked by {interaction.user}")
        # Open the Modal for editing
        modal = HandwrittenMemoEditModal(
            self.cog,
            self.original_question,
            self.recognized_text,
            self.original_reply_message
        )
        await interaction.response.send_modal(modal)
        # Disable the button after click to prevent multiple modals
        self.children[0].disabled = True
        await interaction.message.edit(view=self)
        self.stop() # Stop the view after opening the modal

    async def on_timeout(self):
        logging.info("EditHandwrittenView timed out.")
        # Optionally edit the original message to remove the view on timeout
        # This requires fetching the message again as interaction is not available here.


# ==============================================================================
# === ZeroSecondThinkingCog ====================================================
# ==============================================================================

class ZeroSecondThinkingCog(commands.Cog):
    """
    Discord上でゼロ秒思考を支援するためのCog
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # --- 環境変数からの設定読み込み ---
        self.channel_id = int(os.getenv("ZERO_SECOND_THINKING_CHANNEL_ID", "0"))
        self.openai_api_key = os.getenv("OPENAI_API_KEY") # 音声入力用
        self.gemini_api_key = os.getenv("GEMINI_API_KEY") # テキスト生成・画像認識用

        # Dropbox設定
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.history_path = f"{self.dropbox_vault_path}/.bot/zero_second_thinking_history.json"

        # --- 状態管理 ---
        # message_id と question_text を保持する辞書
        self.active_questions = {} # {message_id: question_text}
        # 最後に生成した質問のメッセージID（ループでの重複投稿防止などに使う）
        self.last_generated_question_id = None

        # --- 初期チェックとAPIクライアント初期化 ---
        if not all([self.channel_id, self.openai_api_key, self.gemini_api_key, self.dropbox_refresh_token]):
            logging.warning("ZeroSecondThinkingCog: 必要な環境変数が不足しています。")
            self.is_ready = False
        else:
            try:
                self.session = aiohttp.ClientSession()
            except Exception as e:
                 logging.error(f"aiohttp ClientSessionの初期化に失敗: {e}")
                 self.is_ready = False
                 return

            self.openai_client = openai.AsyncOpenAI(api_key=self.openai_api_key)
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro") # メインのテキスト生成モデル
            self.gemini_vision_model = genai.GenerativeModel("gemini-2.5-pro") # 画像認識用モデル
            try:
                self.dbx = dropbox.Dropbox(oauth2_refresh_token=self.dropbox_refresh_token, app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret)
                self.dbx.users_get_current_account() # Test connection
            except Exception as e:
                 logging.error(f"Dropbox client initialization failed: {e}", exc_info=True)
                 self.is_ready = False
                 return # Dropbox connection is essential

            self.is_ready = True


    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready:
            self.thinking_prompt_loop.start()
            logging.info(f"ゼロ秒思考の定時通知タスクを開始しました。")

    async def cog_unload(self):
        """Cogのアンロード時にセッションを閉じる"""
        if self.is_ready:
            if hasattr(self, 'session') and self.session and not self.session.closed:
                await self.session.close()
            self.thinking_prompt_loop.cancel()

    async def _get_thinking_history(self) -> list:
        """過去の思考履歴をDropboxから読み込む"""
        try:
            _, res = self.dbx.files_download(self.history_path)
            # Ensure the loaded data is a list
            data = json.loads(res.content.decode('utf-8'))
            return data if isinstance(data, list) else []
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                return [] # Return empty list if file not found
            logging.error(f"思考履歴の読み込みに失敗: {e}")
            return []
        except json.JSONDecodeError:
            logging.error(f"思考履歴ファイル ({self.history_path}) のJSON形式が不正です。空のリストを返します。")
            # Optionally try to clear/reset the corrupted file here
            return []
        except Exception as e: # Catch other potential errors
            logging.error(f"思考履歴の読み込み中に予期せぬエラー: {e}", exc_info=True)
            return []


    async def _save_thinking_history(self, history: list):
        """思考履歴をDropboxに保存（最新10件まで）"""
        try:
            limited_history = history[-10:] # Keep only the last 10 entries
            # Use asyncio.to_thread for blocking Dropbox call
            await asyncio.to_thread(
                self.dbx.files_upload,
                json.dumps(limited_history, ensure_ascii=False, indent=2).encode('utf-8'),
                self.history_path,
                mode=WriteMode('overwrite')
            )
        except Exception as e:
            logging.error(f"思考履歴の保存に失敗: {e}", exc_info=True) # Log traceback

    @tasks.loop(time=THINKING_TIMES)
    async def thinking_prompt_loop(self):
        """定時にお題を投稿するループ"""
        if not self.is_ready: return # Do nothing if not ready

        channel = self.bot.get_channel(self.channel_id)
        if not channel:
             logging.error(f"Zero-Second Thinking channel not found: ID {self.channel_id}")
             return

        try:
            # --- MODIFICATION: Remove deletion of old unanswered questions ---
            # if not self.last_question_answered and self.latest_question_message_id:
            #     try:
            #         old_question_msg = await channel.fetch_message(self.latest_question_message_id)
            #         await old_question_msg.delete()
            #         logging.info(f"未回答の質問 (ID: {self.latest_question_message_id}) を削除しました。")
            #         self.latest_question_message_id = None
            #     except ...
            # -----------------------------------------------------------------

            history = await self._get_thinking_history()
            # Format history context for the prompt
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
            question = "デフォルトのお題: 今、一番気になっていることは何ですか？" # Fallback question
            if response and hasattr(response, 'text') and response.text.strip():
                 question = response.text.strip().replace("*", "") # Remove markdown emphasis
            else:
                 logging.warning(f"Geminiからの質問生成に失敗、または空の応答: {response}")


            embed = discord.Embed(title="🤔 ゼロ秒思考の時間です", description=f"お題: **{question}**", color=discord.Color.teal())
            embed.set_footer(text="このメッセージに返信する形で、思考を書き出してください（音声・手書きメモ画像も可）。`/zst_end`で終了。")

            sent_message = await channel.send(embed=embed)

            # Store the new question and its ID
            self.active_questions[sent_message.id] = question
            self.last_generated_question_id = sent_message.id # Track the latest generated one
            logging.info(f"New thinking question posted: ID {sent_message.id}, Q: {question}")

        except Exception as e:
            logging.error(f"[Zero-Second Thinking] 定時お題生成エラー: {e}", exc_info=True)


    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """メッセージ投稿を監視し、Zero-Second Thinkingのフローを処理する"""
        if not self.is_ready or message.author.bot or message.channel.id != self.channel_id:
            return

        # Handle end command
        if message.content.strip().lower() == "/zst_end":
            await self.end_thinking_session(message)
            return

        # Check if it's a reply to one of the active questions
        if not message.reference or not message.reference.message_id:
            return

        original_message_id = message.reference.message_id

        # --- MODIFICATION: Check against all active questions ---
        if original_message_id not in self.active_questions:
             # logging.debug(f"Message {message.id} is not a reply to an active ZST question.")
             # It might be a reply to the bot's "Edit this text" message, handle later if needed
             return
        # --- End modification ---

        channel = message.channel # Already have the channel object

        try:
            # Fetch the original question message to confirm embed (optional but good practice)
            original_msg = await channel.fetch_message(original_message_id)
            if not original_msg.embeds: return # Ensure it has the expected embed
            embed_title = original_msg.embeds[0].title
            # Allow replies to initial questions or follow-up questions
            if "ゼロ秒思考の時間です" not in embed_title and "さらに深掘りしましょう" not in embed_title:
                return
        except (discord.NotFound, discord.Forbidden):
             logging.warning(f"Could not fetch original question message {original_message_id} for verification.")
             # Continue processing based on active_questions dictionary
             pass # Don't return, process based on stored question
        except Exception as e_fetch:
             logging.error(f"Error fetching original message {original_message_id}: {e_fetch}", exc_info=True)
             return # Stop if fetch fails unexpectedly


        # --- MODIFICATION: Get question from stored dictionary ---
        # last_question = "不明なお題"
        # last_question_match = re.search(r'お題: \*\*(.+?)\*\*', original_msg.embeds[0].description)
        # if last_question_match: last_question = last_question_match.group(1)
        original_question_text = self.active_questions.get(original_message_id, "不明なお題")
        logging.info(f"Processing reply to question ID {original_message_id}: {original_question_text}")
        # --- End modification ---

        # Remove the question from active list as it's being answered
        # We might need to keep it if editing is involved, handle later
        # self.active_questions.pop(original_message_id, None)

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

        # Check for empty text message
        if input_type == "text" and not message.content.strip():
             logging.info("Empty text reply detected. Ignoring.")
             # Re-add to active questions if needed, or just ignore
             # self.active_questions[original_message_id] = original_question_text # Re-add if popped
             try:
                 await message.add_reaction("❓")
             except discord.HTTPException: pass
             return

        # Start processing
        await self._process_thinking_memo(message, original_question_text, original_message_id, input_type, attachment_to_process)

    async def _process_thinking_memo(self, message: discord.Message, original_question: str, original_question_id: int, input_type: str, attachment: discord.Attachment = None):
        """Processes the user's thinking memo (text, audio, or image). For images, asks for edit confirmation."""
        temp_audio_path = None
        formatted_answer = "回答の処理に失敗しました。"

        # Add initial processing reaction
        try:
            await message.add_reaction(PROCESS_START_EMOJI)
        except discord.HTTPException: pass # Ignore if fails

        try:
            # --- Process input based on type ---
            if input_type == "audio" and attachment:
                logging.info("Processing audio memo...")
                temp_audio_path = Path(f"./temp_audio_{message.id}_{attachment.filename}") # More unique temp name
                async with self.session.get(attachment.url) as resp:
                    if resp.status == 200:
                        with open(temp_audio_path, 'wb') as f: f.write(await resp.read())
                    else: raise Exception(f"音声ファイルのダウンロード失敗: Status {resp.status}")

                with open(temp_audio_path, "rb") as audio_file:
                    transcription = await self.openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file)
                transcribed_text = transcription.text
                logging.info("Audio transcribed successfully.")

                formatting_prompt = (
                    "以下の音声メモの文字起こしを、構造化された箇条書きのMarkdown形式でまとめてください。\n"
                    "箇条書きの本文のみを生成し、前置きや返答は一切含めないでください。\n\n"
                    f"---\n\n{transcribed_text}"
                )
                response = await self.gemini_model.generate_content_async(formatting_prompt)
                formatted_answer = response.text.strip() if response and hasattr(response, 'text') else transcribed_text
                logging.info("Audio memo formatted.")

                # --- Directly save and continue for audio ---
                await self._save_and_continue_thinking(
                    message, # Pass original message for context/replying
                    original_question,
                    formatted_answer,
                    message, # Pass message itself again for context
                    input_type
                )
                # --- End audio processing ---

            # >>>>>>>>>>>>>>>>>> MODIFICATION START (Handwritten Memo Edit Flow) <<<<<<<<<<<<<<<<<<
            elif input_type == "image" and attachment:
                logging.info("Processing image memo (handwritten)...")
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

                # --- Send text for confirmation and editing ---
                await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user) # Remove hourglass

                edit_view = EditHandwrittenView(self, original_question, recognized_text, message) # Pass message
                await message.reply(
                    f"**📝 認識された手書きメモ:**\n```markdown\n{recognized_text}\n```\n内容を確認し、必要であれば下のボタンから編集してください。",
                    view=edit_view
                )
                logging.info(f"Sent recognized text and edit button for message {message.id}")
                # --- Stop processing here, wait for button interaction ---
                return # Don't proceed to saving yet

            # >>>>>>>>>>>>>>>>>> MODIFICATION END <<<<<<<<<<<<<<<<<<

            else: # Text input
                logging.info("Processing text memo...")
                formatted_answer = message.content.strip()
                 # --- Directly save and continue for text ---
                await self._save_and_continue_thinking(
                    message, # Pass original message
                    original_question,
                    formatted_answer,
                    message, # Pass message itself again for context
                    input_type
                )
                # --- End text processing ---

        except Exception as e:
            logging.error(f"[Zero-Second Thinking] Error in _process_thinking_memo: {e}", exc_info=True)
            # Remove processing reaction and add error reaction
            try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except discord.HTTPException: pass
            try: await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass
            # Optionally send an error reply to the user
            try: await message.reply(f"❌ メモの処理中にエラーが発生しました: {e}")
            except discord.HTTPException: pass # Ignore if replying fails
        finally:
            # Clean up temporary audio file if it exists
            if temp_audio_path and os.path.exists(temp_audio_path):
                try:
                    os.remove(temp_audio_path)
                    logging.info(f"Temporary audio file removed: {temp_audio_path}")
                except OSError as e_rm:
                     logging.error(f"一時音声ファイル削除失敗: {e_rm}")


    # >>>>>>>>>>>>>>>>>> MODIFICATION START (New function for saving) <<<<<<<<<<<<<<<<<<
    async def _save_and_continue_thinking(self, interaction_or_message, original_question: str, final_answer: str, context_message: discord.Message, input_type: str):
        """Saves the final answer, updates history, saves to Obsidian/GDocs, and asks a follow-up question."""
        original_question_id = context_message.reference.message_id if context_message.reference else None

        # Determine how to respond (followup for interaction, reply for message)
        async def send_followup(content, **kwargs):
            if isinstance(interaction_or_message, discord.Interaction):
                 # Use followup if the initial response was deferred
                 if interaction_or_message.response.is_done():
                     await interaction_or_message.followup.send(content, **kwargs)
                 else: # If not deferred (e.g., modal submit), send response message
                      await interaction_or_message.response.send_message(content, **kwargs)
            elif isinstance(interaction_or_message, discord.Message):
                 await interaction_or_message.reply(content, **kwargs)
            else: # Fallback for context_message if needed
                 await context_message.reply(content, **kwargs)


        try:
            # Add processing reaction to the original user reply message
            if isinstance(context_message, discord.Message):
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
                f"- **Source:** Discord ({input_type.capitalize()})\n" # Use modified input_type
                f"- **作成日:** {daily_note_date}\n\n"
                f"[[{daily_note_date}]]\n\n"
                f"---\n\n"
                f"## 回答\n{final_answer}" # Use the final (potentially edited) answer
            )
            # Use asyncio.to_thread for Dropbox upload
            await asyncio.to_thread(self.dbx.files_upload, new_note_content.encode('utf-8'), note_path, mode=WriteMode('add'))
            logging.info(f"[Zero-Second Thinking] 新規ノートを作成: {note_path}")

            # --- Add link to Daily Note ---
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"
            daily_note_content = ""
            try:
                # Use asyncio.to_thread for Dropbox download
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
            # Use asyncio.to_thread for Dropbox upload
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
                    # Add a warning reaction or message? For now, just log.

            # --- Add completion reaction to the user's reply message ---
            if isinstance(context_message, discord.Message):
                 try:
                     await context_message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                     await context_message.add_reaction(PROCESS_COMPLETE_EMOJI)
                 except discord.HTTPException: pass # Ignore reaction errors

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
            """ # Added "(質問文のみ)"
            response = await self.gemini_model.generate_content_async(digging_prompt)
            new_question = "追加の質問: さらに詳しく教えてください。" # Fallback
            if response and hasattr(response, 'text') and response.text.strip():
                 # Attempt to clean up potential markdown or extra phrases
                 potential_question = response.text.strip().replace("*", "")
                 # Simple check if it looks like just a question
                 if "?" in potential_question and len(potential_question.split('\n')) == 1:
                      new_question = potential_question
                 else: # Fallback if formatting is unexpected
                      logging.warning(f"Unexpected format for follow-up question: {potential_question}. Using fallback.")
            else:
                 logging.warning(f"Geminiからの深掘り質問生成に失敗、または空の応答: {response}")

            embed = discord.Embed(title="🤔 さらに深掘りしましょう", description=f"お題: **{new_question}**", color=discord.Color.blue())
            embed.set_footer(text="このメッセージに返信する形で、思考を書き出してください。`/zst_end`で終了。")

            # Send the follow-up question in the channel
            channel = context_message.channel # Get channel from context
            sent_message = await channel.send(embed=embed)

            # Store the new follow-up question
            self.active_questions[sent_message.id] = new_question
            self.last_generated_question_id = sent_message.id # Track the latest
            logging.info(f"Follow-up question posted: ID {sent_message.id}, Q: {new_question}")

        except Exception as e_save:
            logging.error(f"[Save/Continue Error] Error saving memo or asking follow-up: {e_save}", exc_info=True)
            # Send error via followup/reply
            await send_followup(f"❌ メモの保存または次の質問の生成中にエラーが発生しました: {e_save}", ephemeral=True)
            # Add error reaction to the original user reply
            if isinstance(context_message, discord.Message):
                 try: await context_message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                 except discord.HTTPException: pass
                 try: await context_message.add_reaction(PROCESS_ERROR_EMOJI)
                 except discord.HTTPException: pass
            # Try to remove question from active list if it failed during processing
            if original_question_id:
                self.active_questions.pop(original_question_id, None)

    # >>>>>>>>>>>>>>>>>> MODIFICATION END <<<<<<<<<<<<<<<<<<


    # --- /zst_end コマンド処理用メソッド ---
    async def end_thinking_session(self, message: discord.Message):
        """ゼロ秒思考セッションを終了する"""
        channel = message.channel
        # --- MODIFICATION: End command logic simplified ---
        # Simply clear the last generated question ID and active questions related to this channel/user maybe?
        # For simplicity, let's just clear the last generated ID to prevent loop issues if it wasn't answered.
        # Active questions dictionary allows users to answer old ones anyway.
        if self.last_generated_question_id:
             # Check if the last generated question is still in the active list (i.e., unanswered)
             if self.last_generated_question_id in self.active_questions:
                 logging.info(f"User ended session. Last question {self.last_generated_question_id} might be left unanswered.")
                 # Optionally remove it from active_questions here if desired
                 # self.active_questions.pop(self.last_generated_question_id, None)

        self.last_generated_question_id = None # Reset the last generated ID tracker

        # Remove the /zst_end message itself
        try:
            await message.delete()
        except discord.HTTPException: pass
        # Send a confirmation message that auto-deletes
        try:
            await channel.send("ゼロ秒思考セッションの状態をリセットしました（過去の質問には引き続き回答できます）。", delete_after=10)
        except discord.HTTPException: pass
        logging.info("User requested /zst_end. State reset.")
        # --- End modification ---


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
    # Add the cog only if it's ready
    if cog_instance.is_ready:
        await bot.add_cog(cog_instance)
    else:
        logging.error("ZeroSecondThinkingCog failed to initialize properly and was not loaded.")