# cogs/zero-second_thinking_cog.py (修正版)
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
from pathlib import Path # ★ pathlib をインポート
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

    # ★ 修正: original_question_id を受け取る
    def __init__(self, cog, original_question_id: int, original_question: str, initial_text: str, user_reply_message: discord.Message, bot_confirm_message: discord.Message, input_type_suffix: str):
        super().__init__(timeout=1800) # 30分
        self.cog = cog
        self.original_question_id = original_question_id
        self.original_question = original_question
        self.memo_text.default = initial_text # Pre-fill
        self.user_reply_message = user_reply_message
        self.bot_confirm_message = bot_confirm_message
        self.input_type_suffix = input_type_suffix # e.g., "(edited audio)" or "(edited image)"

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception as e_defer:
            logging.error(f"TextEditModal: on_submitでのdeferに失敗 (ネットワークエラーの可能性): {e_defer}", exc_info=True)
            pass

        edited_text = self.memo_text.value
        logging.info(f"Memo edited and submitted by {interaction.user} (Type: {self.input_type_suffix}).")
        try:
            # 1. Obsidianへの保存処理 (★ 修正: クリーンアップしない関数を呼ぶ)
            await self.cog._save_memo_to_obsidian(
                self.original_question,
                edited_text,
                f"{self.input_type_suffix}", # Pass the specific edited type
                self.user_reply_message.author # ★ 修正: authorを渡す
            )
            
            # 2. 完了メッセージ (★ 修正: delete_after を削除)
            await interaction.followup.send("✅ 編集されたメモを処理しました。", ephemeral=True)

            # 3. フォローアップの質問 (★ 修正: 処理をここに移動)
            await self.cog._ask_followup_question(
                self.user_reply_message, # context_message
                self.original_question,
                edited_text,
                self.original_question_id
            )

            # 4. メッセージのクリーンアップ (★ 修正: 成功応答の後に移動)
            try:
                await self.bot_confirm_message.delete()
                await self.user_reply_message.delete() # user_reply_message は元のメモ
                logging.info(f"Cleanup successful for edited memo (Orig ID: {self.user_reply_message.id})")
            except discord.HTTPException as e_del:
                logging.warning(f"Failed to cleanup messages after edit: {e_del}")


        except Exception as e:
            logging.error(f"Error processing edited memo (Type: {self.input_type_suffix}): {e}", exc_info=True)
            try:
                await interaction.followup.send(f"❌ 編集されたメモの処理中にエラーが発生しました: {e}", ephemeral=True)
            except discord.HTTPException as e_followup:
                logging.error(f"TextEditModal: エラーのfollowup送信にも失敗: {e_followup}")
            try: await self.user_reply_message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logging.error(f"Error in TextEditModal: {error}", exc_info=True)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ モーダルの処理中にエラーが発生しました: {error}", ephemeral=True)
            else:
                 try: await interaction.response.send_message(f"❌ モーダルの処理中にエラーが発生しました: {error}", ephemeral=True)
                 except discord.InteractionResponded: pass
        except discord.HTTPException as e_resp:
             logging.error(f"TextEditModal: on_errorでの応答送信に失敗: {e_resp}")
        try: await self.user_reply_message.add_reaction(PROCESS_ERROR_EMOJI)
        except discord.HTTPException: pass

# --- View for Confirming/Editing Text (Used for both Audio and Image) ---
class ConfirmTextView(discord.ui.View):
    # ★ 修正: original_question_id を受け取る
    def __init__(self, cog, original_question_id: int, original_question: str, recognized_text: str, user_reply_message: discord.Message, input_type_raw: str):
        super().__init__(timeout=3600) # 1時間
        self.cog = cog
        self.original_question_id = original_question_id
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
            # 1. Obsidianへの保存処理 (★ 修正: クリーンアップしない関数を呼ぶ)
            await self.cog._save_memo_to_obsidian(
                self.original_question,
                self.recognized_text,
                f"{self.input_type_raw} (confirmed)",
                self.user_reply_message.author # ★ 修正: authorを渡す
            )
            
            # 2. 完了メッセージ (★ 修正: delete_after を削除)
            await interaction.followup.send("✅ メモを処理しました。", ephemeral=True)

            # 3. フォローアップの質問 (★ 修正: 処理をここに移動)
            await self.cog._ask_followup_question(
                self.user_reply_message, # context_message
                self.original_question,
                self.recognized_text,
                self.original_question_id
            )

            # 4. メッセージのクリーンアップ (★ 修正: 成功応答の後に移動)
            try:
                await self.bot_confirm_message.delete()
                await self.user_reply_message.delete()
                logging.info(f"Cleanup successful for confirmed memo (Orig ID: {self.user_reply_message.id})")
            except discord.HTTPException as e_del:
                logging.warning(f"Failed to cleanup messages after confirm: {e_del}")

        except Exception as e:
            logging.error(f"Error processing confirmed memo (Type: {self.input_type_raw}): {e}", exc_info=True)
            try:
                await interaction.followup.send(f"❌ 確認されたメモの処理中にエラーが発生しました: {e}", ephemeral=True)
            except discord.HTTPException as e_followup:
                 logging.error(f"ConfirmTextView: エラーのfollowup送信にも失敗: {e_followup}")
            try: await self.user_reply_message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass
        finally:
             self.stop()

    @discord.ui.button(label="編集する", style=discord.ButtonStyle.primary, custom_id="edit_text")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        logging.info(f"Edit button clicked by {interaction.user} (Type: {self.input_type_raw})")
        # Open the Modal for editing
        # ★ 修正: original_question_id を渡す
        modal = TextEditModal(
            self.cog,
            self.original_question_id,
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
# === ★ 新規追加: ZSTSelectView (お題選択用ドロップダウン) ========================
# ==============================================================================

class ZSTSelectView(discord.ui.View):
    def __init__(self, 
                 cog, 
                 question_options: list[discord.SelectOption], 
                 original_context: discord.Message, 
                 attachment: discord.Attachment = None, 
                 text_memo: str = None, 
                 input_type: str = None
                 ):
        super().__init__(timeout=600) # 10分
        self.cog = cog
        self.original_context = original_context # ユーザーが投稿したメモのMessage
        self.attachment = attachment
        self.text_memo = text_memo
        self.input_type = input_type
        self.bot_reply_message = None # ボットが送信したこのViewを含むメッセージ

        placeholder_text = f"この {input_type} メモはどのお題に対する回答ですか？"
        
        select = discord.ui.Select(
            placeholder=placeholder_text,
            options=question_options,
            custom_id="zst_question_select"
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        # 選択されたお題（のメッセージID）
        selected_q_id = int(interaction.data["values"][0])
        
        # 選択されたお題（のテキスト）
        try:
            selected_q_text = self.cog.active_questions[selected_q_id]
        except KeyError:
             logging.error(f"ZSTSelectView: 選択されたお題ID {selected_q_id} がアクティブリストに見つかりません。")
             await interaction.response.send_message("エラー: 選択されたお題が見つかりませんでした。お題が終了した可能性があります。", ephemeral=True)
             if self.bot_reply_message:
                 await self.bot_reply_message.edit(content="エラー: 選択されたお題が見つかりませんでした。", view=None)
             return

        await interaction.response.defer(ephemeral=True, thinking=True)
        
        # ドロップダウンメッセージを「処理中...」に変更
        if self.bot_reply_message:
            try:
                await self.bot_reply_message.edit(content=f"お題「{selected_q_text[:50]}...」への {self.input_type} メモを処理中です... {PROCESS_START_EMOJI}", view=None)
            except discord.HTTPException as e_edit:
                 logging.warning(f"ZSTSelectView: ドロップダウンメッセージの編集に失敗: {e_edit}")

        # テキスト化処理を呼び出す
        await self.cog.process_posted_memo(
            interaction, 
            self.original_context, # 元のファイル添付/テキストメッセージ
            selected_q_id,
            selected_q_text,
            self.input_type,
            self.attachment, # None if text
            self.text_memo,   # None if attachment
            self.bot_reply_message # このView(ドロップダウン)のメッセージ
        )
        
        self.stop()

    async def on_timeout(self):
        # ボットが送信したドロップダウンメッセージを編集
        if self.bot_reply_message:
            try:
                await self.bot_reply_message.edit(content="お題の選択がタイムアウトしました。", view=None)
            except discord.HTTPException as e:
                logging.warning(f"ZSTSelectView: タイムアウトメッセージの編集に失敗: {e}")


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
        # ★ 修正: {message_id: question_text}
        self.active_questions = {} 
        # (user_last_interaction は /zst_end のために残す)
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
    @app_commands.describe(prompt="お題を自分で設定する場合に入力します（AIによる自動生成を省略）。")
    async def zst_start(self, interaction: discord.Interaction, prompt: str = None):
        """Generates and posts a new thinking prompt, or uses a user-provided one."""
        if not self.is_ready:
            await interaction.response.send_message("ゼロ秒思考機能は現在準備中です。", ephemeral=True)
            return
        if interaction.channel_id != self.channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.channel_id}> でのみ使用できます。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False, thinking=True)

        try:
            question = ""

            if prompt:
                # ユーザーがプロンプトを指定した場合
                question = prompt
                logging.info(f"New thinking question posted via command (User-defined): Q: {question}")
            
            else:
                # ユーザーがプロンプトを指定しなかった場合 (AIが生成)
                history = await self._get_thinking_history()
                history_context = "\n".join([f"- {item.get('question', 'Q')}: {item.get('answer', 'A')[:100]}..." for item in history])

                ai_prompt = f"""
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
                response = await self.gemini_model.generate_content_async(ai_prompt)
                question = "デフォルトのお題: 今、一番気になっていることは何ですか？"
                if response and hasattr(response, 'text') and response.text.strip():
                     question = response.text.strip().replace("*", "")
                else:
                     logging.warning(f"Geminiからの質問生成に失敗、または空の応答: {response}")
                logging.info(f"New thinking question posted via command (AI-generated): Q: {question}")

            # 共通の埋め込み送信処理
            embed = discord.Embed(title="🤔 ゼロ秒思考 - 新しいお題", description=f"お題: **{question}**", color=discord.Color.teal())
            # ★ 修正: フッターの文言を変更
            embed.set_footer(text="このお題に対する思考を、テキスト、音声、または手書きメモ画像で投稿してください。")

            sent_message = await interaction.followup.send(embed=embed)

            # ★ 修正: {message_id: question_text} で保存
            self.active_questions[sent_message.id] = question
            # ★ 修正: ユーザーの最後のインタラクション（お題）も記録
            self.user_last_interaction[interaction.user.id] = sent_message.id 
            logging.info(f"New thinking question active: ID {sent_message.id}, Q: {question}")

        except Exception as e:
            logging.error(f"[Zero-Second Thinking] /zst_start コマンドエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ 質問の生成中にエラーが発生しました: {e}", ephemeral=True)


    # ★ 修正: on_message リスナー (BookCog と同様のロジックに変更)
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """メッセージ投稿を監視し、Zero-Second Thinkingのフローを処理する"""
        if not self.is_ready or message.author.bot or message.channel.id != self.channel_id:
            return

        # スラッシュコマンドは無視
        if message.content.strip().startswith('/'):
             return

        # リプライは無視 (お題のEmbedへのリプライも含む)
        if message.reference:
            return

        # --- 入力タイプを判別 ---
        attachment = None
        input_type = None
        text_memo = None

        if message.attachments:
            # 添付ファイルがある場合
            attachment = message.attachments[0]
            if attachment.content_type in SUPPORTED_AUDIO_TYPES:
                input_type = "audio"
            elif attachment.content_type in SUPPORTED_IMAGE_TYPES:
                input_type = "image"
            else:
                logging.debug(f"ZSTCog: サポート対象外の添付ファイルタイプ: {attachment.content_type}")
                return # サポート対象外のファイル
        else:
            # 添付ファイルがない場合 (テキストメモ)
            text_memo = message.content.strip()
            if not text_memo:
                return # 空のメッセージは無視
            input_type = "text"
        
        logging.info(f"ZSTCog: {input_type} メモを検知: {message.jump_url}")
        
        bot_reply_message = None
        try:
            await message.add_reaction("🤔") # 処理中（どのお題か考えてる）

            # --- アクティブなお題一覧を取得 ---
            if not self.active_questions:
                await message.reply(f"❌ 回答対象のアクティブなお題がありません。`/zst_start` でお題を開始してください。", delete_after=15)
                await message.remove_reaction("🤔", self.bot.user)
                return

            # {msg_id: q_text} からオプションを作成
            options = [
                discord.SelectOption(
                    label=(q_text[:97] + '...') if len(q_text) > 100 else q_text, 
                    value=str(msg_id)
                ) 
                for msg_id, q_text in self.active_questions.items()
            ][:25] # 最大25個
            
            # --- 選択Viewを表示 ---
            view = ZSTSelectView(
                self, 
                options, 
                original_context=message, # 元のメッセージを渡す
                attachment=attachment, 
                text_memo=text_memo,
                input_type=input_type
            )
            bot_reply_message = await message.reply(f"この {input_type} メモはどのお題に対する回答ですか？", view=view, mention_author=False)
            view.bot_reply_message = bot_reply_message # Viewに自身のメッセージをセット
            
        except Exception as e:
            logging.error(f"ZSTCog: on_message での添付ファイル/テキスト処理中にエラー: {e}", exc_info=True)
            if bot_reply_message: # bot_reply_message がNoneでないことを確認
                try: await bot_reply_message.delete()
                except discord.HTTPException: pass
            await message.reply(f"❌ メモの処理開始中にエラーが発生しました: {e}")
            try:
                await message.remove_reaction("🤔", self.bot.user)
                await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException:
                pass

    # ★ 修正: _process_thinking_memo -> process_posted_memo に改名・シグネチャ変更
    async def process_posted_memo(self, 
                                  interaction: discord.Interaction, # SelectViewからのInteraction
                                  original_message: discord.Message, # ユーザーが投稿したメモ
                                  original_question_id: int, 
                                  original_question_text: str, 
                                  input_type: str, 
                                  attachment: discord.Attachment = None, 
                                  text_memo: str = None,
                                  dropdown_message: discord.Message = None):
        """Processes the user's thinking memo. Asks for edit confirmation for audio/image."""
        temp_audio_path = None
        formatted_answer = "回答の処理に失敗しました。"

        try:
            await original_message.add_reaction(PROCESS_START_EMOJI)
        except discord.HTTPException: pass

        try:
            if input_type == "audio" and attachment:
                logging.info("Processing audio memo...")
                # --- Audio Transcription ---
                temp_audio_path = Path(f"./temp_audio_{original_message.id}_{attachment.filename}")
                async with self.session.get(attachment.url) as resp:
                    if resp.status == 200:
                        with open(temp_audio_path, 'wb') as f: f.write(await resp.read())
                    else: raise Exception(f"音声ファイルのダウンロード失敗: Status {resp.status}")
                with open(temp_audio_path, "rb") as audio_file:
                    transcription = await self.openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file)
                transcribed_text = transcription.text
                logging.info("Audio transcribed successfully.")
                # --- End Transcription ---

                # --- ★ 修正: Formatting (箇条書き記号なし) ---
                formatting_prompt = (
                    "以下の音声メモの文字起こしを、構造化されたメモ形式でまとめてください。\n"
                    "**箇条書きの記号（「-」や「*」など）は使用せず**、各項目を改行して並べてください。\n"
                    "返答には前置きや説明は一切含めないでください。\n\n"
                    f"---\n\n{transcribed_text}"
                )
                response = await self.gemini_model.generate_content_async(formatting_prompt)
                formatted_answer = response.text.strip() if response and hasattr(response, 'text') else transcribed_text
                logging.info("Audio memo formatted.")
                # --- ★ 修正ここまで ---

                # --- Send for confirmation ---
                try: await original_message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                except discord.HTTPException: pass
                if dropdown_message:
                    try: await dropdown_message.delete()
                    except discord.HTTPException: pass

                confirm_view = ConfirmTextView(self, original_question_id, original_question_text, formatted_answer, original_message, "audio")
                bot_confirm_msg = await original_message.reply(
                    f"**🎤 認識された音声メモ:**\n```markdown\n{formatted_answer}\n```\n内容を確認し、問題なければ「このまま投稿」、修正する場合は「編集する」ボタンを押してください。",
                    view=confirm_view
                )
                confirm_view.bot_confirm_message = bot_confirm_msg
                logging.info(f"Sent recognized audio text and confirm/edit buttons for message {original_message.id}")
                # ★ 修正: delete_after 削除
                await interaction.followup.send("テキストを認識しました。内容を確認してください。", ephemeral=True)
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
                
                # --- ★ 修正: Vision Prompt (箇条書き記号なし) ---
                vision_prompt = [
                    "この画像は手書きのメモです。内容を読み取り、テキスト化してください。\n"
                    "**箇条書きの記号（「-」や「*」など）は使用せず**、読み取った内容を改行して並べてください。\n"
                    "返答には前置きや説明は含めず、テキスト本体のみを生成してください。",
                    img,
                ]
                # --- ★ 修正ここまで ---
                
                response = await self.gemini_vision_model.generate_content_async(vision_prompt)
                recognized_text = response.text.strip() if response and hasattr(response, 'text') else "手書きメモの読み取りに失敗しました。"
                logging.info("Image memo recognized by Gemini Vision.")
                # --- End Recognition ---

                # --- Send for confirmation ---
                try: await original_message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                except discord.HTTPException: pass
                if dropdown_message:
                    try: await dropdown_message.delete()
                    except discord.HTTPException: pass

                confirm_view = ConfirmTextView(self, original_question_id, original_question_text, recognized_text, original_message, "image")
                bot_confirm_msg = await original_message.reply(
                    f"**📝 認識された手書きメモ:**\n```markdown\n{recognized_text}\n```\n内容を確認し、問題なければ「このまま投稿」、修正する場合は「編集する」ボタンを押してください。",
                    view=confirm_view
                )
                confirm_view.bot_confirm_message = bot_confirm_msg
                logging.info(f"Sent recognized image text and confirm/edit buttons for message {original_message.id}")
                # ★ 修正: delete_after 削除
                await interaction.followup.send("テキストを認識しました。内容を確認してください。", ephemeral=True)
                return # Wait for button interaction
                # --- End Confirmation ---

            else: # Text input
                logging.info("Processing text memo...")
                formatted_answer = text_memo # (text_memo は on_message で .strip() 済み)
                
                if dropdown_message:
                    try: await dropdown_message.delete()
                    except discord.HTTPException: pass
                
                 # 1. 保存
                await self._save_memo_to_obsidian(
                    original_question_text,
                    formatted_answer,
                    input_type,
                    original_message.author
                )
                 # 2. 完了応答 (★ 修正: delete_after 削除)
                await interaction.followup.send("テキストメモを処理しました。", ephemeral=True)
                
                 # 3. フォローアップ
                await self._ask_followup_question(
                    original_message,
                    original_question_text,
                    formatted_answer,
                    original_question_id
                )
                 # 4. クリーンアップ
                try:
                    await original_message.delete()
                    logging.info(f"Cleanup successful for text memo (Orig ID: {original_message.id})")
                except discord.HTTPException as e_del:
                    logging.warning(f"Failed to cleanup text memo: {e_del}")


        except Exception as e:
            logging.error(f"[Zero-Second Thinking] Error in process_posted_memo: {e}", exc_info=True)
            try: await original_message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except discord.HTTPException: pass
            try: await original_message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass
            try:
                # interaction が完了していなければ、そちらで応答
                if not interaction.response.is_done():
                    await interaction.response.send_message(f"❌ メモの処理中にエラーが発生しました: {e}", ephemeral=True)
                else:
                    await interaction.followup.send(f"❌ メモの処理中にエラーが発生しました: {e}", ephemeral=True)
            except discord.HTTPException:
                # フォールバックとして元のメッセージにリプライ
                try: await original_message.reply(f"❌ メモの処理中にエラーが発生しました: {e}")
                except discord.HTTPException: pass
        finally:
            if temp_audio_path and os.path.exists(temp_audio_path):
                try:
                    os.remove(temp_audio_path)
                    logging.info(f"Temporary audio file removed: {temp_audio_path}")
                except OSError as e_rm:
                     logging.error(f"一時音声ファイル削除失敗: {e_rm}")

    # ★ 新規追加: _save_memo_to_obsidian (保存のみ)
    async def _save_memo_to_obsidian(self, original_question: str, final_answer: str, input_type: str, author: discord.User | discord.Member):
        """Saves the final answer, updates history, saves to Obsidian/GDocs."""
        
        try:
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
                    daily_note_content = f"# {date_str}\n"
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
        
        except Exception as e:
            logging.error(f"_save_memo_to_obsidian 処理中にエラー: {e}", exc_info=True)
            raise # エラーを呼び出し元に伝達


    # ★ 新規追加: _ask_followup_question (フォローアップ質問とクリーンアップ)
    async def _ask_followup_question(self, context_message: discord.Message, original_question: str, final_answer: str, original_question_id: int):
        """Asks a follow-up question and manages reactions/cleanup."""
        
        # Determine how to send the follow-up question
        async def send_followup_question(embed):
            # Always send to the channel of the context message
            channel = context_message.channel
            return await channel.send(embed=embed)

        try:
            # --- Add completion reaction ---
            if isinstance(context_message, discord.Message):
                 try:
                     # ⏳ があれば削除
                     await context_message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                     await context_message.add_reaction(PROCESS_COMPLETE_EMOJI)
                 except discord.HTTPException: pass

            # --- Remove original question from active list ---
            if original_question_id:
                popped_question = self.active_questions.pop(original_question_id, None)
                if popped_question:
                    logging.info(f"Question ID {original_question_id} removed from active list.")
                else:
                    logging.warning(f"Question ID {original_question_id} was already removed from active list.")

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
            embed.set_footer(text="このお題に対する思考を投稿してください (テキスト・音声・画像)。`/zst_end`で終了。")

            sent_message = await send_followup_question(embed=embed)

            # Store the new follow-up question
            self.active_questions[sent_message.id] = new_question
            logging.info(f"Follow-up question posted: ID {sent_message.id}, Q: {new_question}")
            
            # ユーザーの最後のインタラクション（お題）も更新
            self.user_last_interaction[context_message.author.id] = sent_message.id

            # (テキスト入力の場合、この時点で context_message は削除済み)
            # (音声/画像の場合、クリーンアップは呼び出し元 (View/Modal) が行う)

        except Exception as e_followup:
            logging.error(f"[Zero-Second Thinking] Error asking follow-up question: {e_followup}", exc_info=True)
            try:
                await context_message.reply(f"❌ 次の質問の生成中にエラーが発生しました: {e_followup}")
            except discord.HTTPException:
                pass
            if isinstance(context_message, discord.Message):
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
        
        # ★ 修正: ユーザーが最後に操作したお題IDを取得
        last_interacted_question_id = self.user_last_interaction.pop(user_id, None)

        # Remove any active questions associated with the last interaction (if applicable)
        if last_interacted_question_id:
             # ★ 修正: 該当のお題IDをアクティブリストから削除
             popped_question = self.active_questions.pop(last_interacted_question_id, None)
             if popped_question:
                 logging.info(f"User {user_id} ended their ZST session. Cleared state for question {last_interacted_question_id} ('{popped_question}').")
             else:
                 logging.info(f"User {user_id} used /zst_end, but question {last_interacted_question_id} was already inactive.")
        else:
             logging.info(f"User {user_id} used /zst_end but had no active interaction tracked.")
        
        # ★ 修正: delete_after 削除
        await interaction.response.send_message("ゼロ秒思考セッションを終了しました。新しいお題は `/zst_start` で始められます。", ephemeral=True)


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