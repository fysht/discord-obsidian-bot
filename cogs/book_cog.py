import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import asyncio
import dropbox
from dropbox.files import WriteMode, DownloadError, FileMetadata 
from dropbox.exceptions import ApiError
import datetime
import zoneinfo
import aiohttp
import urllib.parse
import openai # ★ (1) 音声認識 (Whisper) のために追加
from PIL import Image # ★ (2) 画像処理のために追加
import io
import pathlib

# 共通関数をインポート
try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.warning("BookCog: utils/obsidian_utils.pyが見つからないため、簡易的な追記処理を使用します。")
    # 簡易的なダミー関数 (フォールバック)
    def update_section(current_content: str, text_to_add: str, section_header: str) -> str:
        # (簡易的な追記処理)
        return f"{current_content.strip()}\n\n{section_header}\n{text_to_add}\n"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
READING_NOTES_PATH = "/Reading Notes" 

# --- リアクション定数 ---
BOT_PROCESS_TRIGGER_REACTION = '📥' 
PROCESS_START_EMOJI = '⏳'
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'
API_ERROR_EMOJI = '☁️'
NOT_FOUND_EMOJI = '🧐'

# --- ステータス定義 ---
STATUS_OPTIONS = {
    "to_read": "To Read",
    "reading": "Reading",
    "finished": "Finished"
}

# ★ (3) 対応するファイルタイプ (zero-second_thinking_cog.py から流用)
SUPPORTED_AUDIO_TYPES = [
    'audio/mpeg', 'audio/x-m4a', 'audio/ogg', 'audio/wav', 'audio/webm'
]
SUPPORTED_IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp']

# ★ (4) HEIC/HEIF対応 (オプション)
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    SUPPORTED_IMAGE_TYPES.extend(['image/heic', 'image/heif'])
    logging.info("BookCog: HEIC/HEIF image support enabled.")
except ImportError:
    logging.warning("BookCog: pillow_heif not installed. HEIC/HEIF support is disabled.")


# --- メモ入力用モーダル (変更なし) ---
class BookMemoModal(discord.ui.Modal, title="読書メモの入力"):
    memo_text = discord.ui.TextInput(
        label="書籍に関するメモを入力してください",
        style=discord.TextStyle.paragraph,
        placeholder="例: p.56 〇〇という視点は新しい...",
        required=True,
        max_length=1500
    )

    def __init__(self, cog, selected_book_path: str):
        super().__init__(timeout=1800)
        self.cog = cog
        self.book_path = selected_book_path

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            _, res = await asyncio.to_thread(self.cog.dbx.files_download, self.book_path)
            current_content = res.content.decode('utf-8')
            now = datetime.datetime.now(JST)
            time_str = now.strftime('%H:%M')
            memo_lines = self.memo_text.value.strip().split('\n')
            formatted_memo = f"- {time_str}\n\t- " + "\n\t- ".join(memo_lines)
            section_header = "## メモ"
            new_content = update_section(current_content, formatted_memo, section_header)
            await asyncio.to_thread(
                self.cog.dbx.files_upload,
                new_content.encode('utf-8'),
                self.book_path,
                mode=WriteMode('overwrite')
            )
            logging.info(f"BookCog: 読書メモを追記しました: {self.book_path}")
            await interaction.followup.send(f"✅ テキストメモを追記しました。\n`{os.path.basename(self.book_path)}`", ephemeral=True)
        except Exception as e:
            logging.error(f"BookCog: 読書メモ追記中のエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ メモ追記中にエラーが発生しました: {e}", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logging.error(f"Error in BookMemoModal: {error}", exc_info=True)
        if interaction.response.is_done(): await interaction.followup.send(f"❌ モーダル処理中にエラー: {error}", ephemeral=True)
        else:
            try: await interaction.response.send_message(f"❌ モーダル処理中にエラー: {error}", ephemeral=True)
            except discord.InteractionResponded: pass

# --- ステータス変更用ボタンView (★ 修正: original_context を受け取る) ---
class BookStatusView(discord.ui.View):
    def __init__(self, cog, book_path: str, original_context: discord.Interaction | discord.Message):
        super().__init__(timeout=300) 
        self.cog = cog
        self.book_path = book_path
        self.original_context = original_context # 元の /book_status コマンドのインタラクション or メッセージ

    async def _delete_original_context(self):
        """インタラクションかメッセージかに応じて元のUIを削除する"""
        try:
            if isinstance(self.original_context, discord.Interaction):
                await self.original_context.delete_original_response()
            elif isinstance(self.original_context, discord.Message):
                await self.original_context.delete()
        except discord.HTTPException:
            logging.warning("BookStatusView: 元のコンテキストメッセージの削除に失敗しました。")

    @discord.ui.button(label="積読 (To Read)", style=discord.ButtonStyle.secondary, emoji="📚", custom_id="status_to_read")
    async def to_read_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_status_change(interaction, STATUS_OPTIONS["to_read"])

    @discord.ui.button(label="読書中 (Reading)", style=discord.ButtonStyle.primary, emoji="📖", custom_id="status_reading")
    async def reading_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_status_change(interaction, STATUS_OPTIONS["reading"])

    @discord.ui.button(label="読了 (Finished)", style=discord.ButtonStyle.success, emoji="✅", custom_id="status_finished")
    async def finished_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_status_change(interaction, STATUS_OPTIONS["finished"])

    async def handle_status_change(self, interaction: discord.Interaction, new_status: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            success = await self.cog._update_book_status(self.book_path, new_status)
            if success:
                book_name = os.path.basename(self.book_path)
                await interaction.followup.send(f"✅ ステータスを変更しました。\n`{book_name}` -> **{new_status}**", ephemeral=True)
                # 元の選択Viewメッセージを削除
                await self._delete_original_context()
            else:
                await interaction.followup.send(f"❌ ステータス変更に失敗しました。", ephemeral=True)
        except Exception as e:
            logging.error(f"BookStatusView: ステータス変更処理中にエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ ステータス変更中に予期せぬエラーが発生しました: {e}", ephemeral=True)
        finally:
            self.stop() 

    async def on_timeout(self):
        try:
            if isinstance(self.original_context, discord.Interaction):
                await self.original_context.edit_original_response(content="ステータス変更がタイムアウトしました。", view=None)
            elif isinstance(self.original_context, discord.Message):
                await self.original_context.edit(content="ステータス変更がタイムアウトしました。", view=None)
        except discord.HTTPException:
            pass

# --- 書籍選択用ドロップダウン (★ 修正: 汎用化) ---
class BookSelectView(discord.ui.View):
    def __init__(self, 
                 cog, 
                 book_options: list[discord.SelectOption], 
                 original_context: discord.Interaction | discord.Message, 
                 action_type: str, 
                 attachment: discord.Attachment = None, 
                 input_type: str = None
                 ):
        super().__init__(timeout=600) # 10分
        self.cog = cog
        self.original_context = original_context # Interaction または Message
        self.action_type = action_type # "memo", "status", "attachment"
        self.attachment = attachment # attachment の場合
        self.input_type = input_type # "audio" or "image"
        
        placeholder_text = "操作対象の書籍を選択してください..."
        if action_type == "memo":
            placeholder_text = "メモを追記する書籍を選択..."
        elif action_type == "status":
            placeholder_text = "ステータスを変更する書籍を選択..."
        elif action_type == "attachment":
            placeholder_text = f"この{input_type}メモを追記する書籍を選択..."

        select = discord.ui.Select(
            placeholder=placeholder_text,
            options=book_options,
            custom_id="book_select"
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def _edit_original_response(self, **kwargs):
        """Context (Interaction or Message) に応じて応答を編集する"""
        try:
            if isinstance(self.original_context, discord.Interaction):
                await self.original_context.edit_original_response(**kwargs)
            elif isinstance(self.original_context, discord.Message):
                await self.original_context.edit(**kwargs)
        except discord.HTTPException as e:
            logging.warning(f"BookSelectView: 元のメッセージの編集に失敗: {e}")

    async def select_callback(self, interaction: discord.Interaction):
        selected_path = interaction.data["values"][0]
        
        if self.action_type == "memo":
            # テキストメモ入力モーダルを表示
            modal = BookMemoModal(self.cog, selected_path)
            await interaction.response.send_modal(modal)
            # 元のドロップダウンメッセージを削除
            await self._edit_original_response(content="テキストメモを入力中です...", view=None)

        elif self.action_type == "status":
            # ステータス変更ボタンViewを表示
            selected_option_label = next((opt.label for opt in interaction.message.components[0].children[0].options if opt.value == selected_path), "選択された書籍")
            
            status_view = BookStatusView(self.cog, selected_path, self.original_context)
            
            await interaction.response.edit_message(
                content=f"**{selected_option_label}** のステータスを選択してください:",
                view=status_view
            )

        elif self.action_type == "attachment":
            # 添付ファイルの処理を開始
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self._edit_original_response(content=f"`{os.path.basename(selected_path)}` に {self.input_type} メモを処理中です... {PROCESS_START_EMOJI}", view=None)
            
            await self.cog.process_attached_memo(
                interaction, 
                self.original_context, # 元のファイル添付メッセージ
                selected_path, 
                self.attachment, 
                self.input_type
            )
        
        self.stop() # ドロップダウンViewは停止

    async def on_timeout(self):
        await self._edit_original_response(content="書籍の選択がタイムアウトしました。", view=None)


class BookCog(commands.Cog):
    """Google Books APIと連携し、読書ノートを作成するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.book_note_channel_id = int(os.getenv("BOOK_NOTE_CHANNEL_ID", 0))
        self.google_books_api_key = os.getenv("GOOGLE_BOOKS_API_KEY")
        # ★ (5) OpenAI / Gemini APIキーを追加
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY") # 既に存在
        
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        
        self.dbx = None
        self.session = None
        self.is_ready = False
        
        # ★ (6) 必須環境変数に OPENAI_API_KEY を追加
        if not all([self.book_note_channel_id, self.google_books_api_key, self.dropbox_refresh_token, self.openai_api_key, self.gemini_api_key]):
            logging.error("BookCog: 必要な環境変数 (BOOK_NOTE_CHANNEL_ID, GOOGLE_BOOKS_API_KEY, DROPBOX_REFRESH_TOKEN, OPENAI_API_KEY, GEMINI_API_KEY) が不足。Cogは動作しません。")
            return

        try:
            self.dbx = dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret, timeout=300
            )
            self.dbx.users_get_current_account()
            logging.info("BookCog: Dropbox client initialized.")

            self.session = aiohttp.ClientSession()
            logging.info("BookCog: aiohttp session started.")
            
            # ★ (7) OpenAI と Gemini Vision クライアントを初期化
            self.openai_client = openai.AsyncOpenAI(api_key=self.openai_api_key)
            genai.configure(api_key=self.gemini_api_key)
            # gemini-2.5-pro は Vision も兼ねている
            self.gemini_vision_model = genai.GenerativeModel("gemini-2.5-pro") 
            
            self.is_ready = True

        except Exception as e:
            logging.error(f"BookCog: Failed to initialize clients: {e}", exc_info=True)

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()
            logging.info("BookCog: aiohttp session closed.")

    async def _update_book_status(self, book_path: str, new_status: str) -> bool:
        # ... (変更なし) ...
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, book_path)
            current_content = res.content.decode('utf-8')
            status_pattern = re.compile(r"^(status:\s*)(\S+.*)$", re.MULTILINE)
            if status_pattern.search(current_content):
                new_content = status_pattern.sub(f"\\g<1>\"{new_status}\"", current_content, count=1)
            else:
                frontmatter_end_pattern = re.compile(r"^(---)$", re.MULTILINE)
                matches = list(frontmatter_end_pattern.finditer(current_content))
                if len(matches) > 1:
                    insert_pos = matches[1].start()
                    new_content = current_content[:insert_pos] + f"status: \"{new_status}\"\n" + current_content[insert_pos:]
                else:
                    return False
            await asyncio.to_thread(
                self.dbx.files_upload,
                new_content.encode('utf-8'),
                book_path,
                mode=WriteMode('overwrite')
            )
            return True
        except Exception as e:
            logging.error(f"BookCog: ステータス更新中のエラー: {e}", exc_info=True)
            return False

    # ★ --- (8) on_message リスナーを新規追加 ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """
        BOOK_NOTE_CHANNEL_ID に投稿された添付ファイルを検知し、
        どの書籍ノートに追記するかをユーザーに尋ねる。
        """
        # --- 基本チェック ---
        if not self.is_ready or message.author.bot or message.channel.id != self.book_note_channel_id:
            return
        # スラッシュコマンドやリプライは無視 (リプライは on_raw_reaction_add や /book_memo で処理)
        if message.content.startswith('/') or message.reference:
            return
        if not message.attachments:
            return

        # --- 添付ファイルタイプの判別 ---
        attachment = message.attachments[0]
        input_type = None
        if attachment.content_type in SUPPORTED_AUDIO_TYPES:
            input_type = "audio"
        elif attachment.content_type in SUPPORTED_IMAGE_TYPES:
            input_type = "image"
        
        if not input_type:
            logging.debug(f"BookCog: サポート対象外の添付ファイルタイプ: {attachment.content_type}")
            return

        logging.info(f"BookCog: {input_type} 添付ファイルを検知: {message.jump_url}")
        
        try:
            await message.add_reaction("🤔") # 処理中（どの本か考えてる）

            # --- 書籍一覧を取得 ---
            book_files, error = await self.get_book_list()
            if error:
                await message.reply(f"❌ {error}")
                await message.remove_reaction("🤔", self.bot.user)
                await message.add_reaction(PROCESS_ERROR_EMOJI)
                return

            options = []
            for entry in book_files[:25]: # 最大25件
                file_name_no_ext = entry.name[:-3]
                label_text = (file_name_no_ext[:97] + '...') if len(file_name_no_ext) > 100 else file_name_no_ext
                options.append(discord.SelectOption(label=label_text, value=entry.path_display))

            # --- 選択Viewを表示 ---
            view = BookSelectView(
                self, 
                options, 
                original_context=message, # 元のメッセージを渡す
                action_type="attachment", 
                attachment=attachment, 
                input_type=input_type
            )
            await message.reply(f"この {input_type} メモはどの書籍のものですか？", view=view, mention_author=False)
            # 🤔 は消さない (ユーザーの選択待ち)

        except Exception as e:
            logging.error(f"BookCog: on_message での添付ファイル処理中にエラー: {e}", exc_info=True)
            await message.reply(f"❌ 添付ファイルの処理開始中にエラーが発生しました: {e}")
            try:
                await message.remove_reaction("🤔", self.bot.user)
                await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException:
                pass

    # ★ --- (9) process_attached_memo メソッドを新規追加 ---
    async def process_attached_memo(
        self, 
        interaction: discord.Interaction, # SelectViewからのInteraction
        original_message: discord.Message, # ユーザーが添付した元のMessage
        book_path: str, 
        attachment: discord.Attachment, 
        input_type: str
    ):
        """添付された音声または画像をテキスト化し、指定されたノートに追記する"""
        
        temp_audio_path = None
        recognized_text = ""
        
        try:
            # 元メッセージのリアクションを更新
            await original_message.remove_reaction("🤔", self.bot.user)
            await original_message.add_reaction(PROCESS_START_EMOJI)

            # 1. ファイルをダウンロード
            async with self.session.get(attachment.url) as resp:
                if resp.status != 200:
                    raise Exception(f"ファイルダウンロード失敗: Status {resp.status}")
                file_bytes = await resp.read()

            # 2. テキスト化
            if input_type == "audio":
                temp_audio_path = pathlib.Path(f"./temp_book_audio_{original_message.id}")
                temp_audio_path.write_bytes(file_bytes)
                
                with open(temp_audio_path, "rb") as audio_file:
                    transcription = await self.openai_client.audio.transcriptions.create(
                        model="whisper-1", 
                        file=audio_file
                    )
                recognized_text = transcription.text
                logging.info(f"BookCog: 音声認識完了 (Whisper): {recognized_text[:50]}...")

            elif input_type == "image":
                img = Image.open(io.BytesIO(file_bytes))
                vision_prompt = [
                    "この画像は手書きのメモです。内容を読み取り、箇条書きのMarkdown形式でテキスト化してください。返答には前置きや説明は含めず、箇条書きのテキスト本体のみを生成してください。",
                    img,
                ]
                response = await self.gemini_vision_model.generate_content_async(vision_prompt)
                recognized_text = response.text.strip()
                logging.info(f"BookCog: 手書きメモ認識完了 (Gemini): {recognized_text[:50]}...")

            if not recognized_text:
                raise Exception("AIによるテキスト化の結果が空でした。")

            # 3. ノートに追記 (BookMemoModal.on_submit と同様のロジック)
            _, res = await asyncio.to_thread(self.dbx.files_download, book_path)
            current_content = res.content.decode('utf-8')
            
            now = datetime.datetime.now(JST)
            time_str = now.strftime('%H:%M')
            memo_lines = recognized_text.strip().split('\n')
            formatted_memo = f"- {time_str} ({input_type} memo)\n\t- " + "\n\t- ".join(memo_lines)
            
            section_header = "## メモ"
            new_content = update_section(current_content, formatted_memo, section_header)
            
            await asyncio.to_thread(
                self.dbx.files_upload,
                new_content.encode('utf-8'),
                book_path,
                mode=WriteMode('overwrite')
            )
            
            logging.info(f"BookCog: {input_type} メモを追記しました: {book_path}")
            await interaction.followup.send(f"✅ {input_type} メモを追記しました。\n`{os.path.basename(book_path)}`", ephemeral=True)
            await original_message.add_reaction(PROCESS_COMPLETE_EMOJI)

        except Exception as e:
            logging.error(f"BookCog: 添付メモ処理中にエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ {input_type} メモの処理中にエラーが発生しました: {e}", ephemeral=True)
            try: await original_message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass
        finally:
            # 終了リアクション
            try: await original_message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except discord.HTTPException: pass
            # 一時ファイルの削除
            if temp_audio_path:
                try: temp_audio_path.unlink()
                except OSError as e_rm: logging.error(f"BookCog: 一時音声ファイルの削除に失敗: {e_rm}")


    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        # (変更なし: 書籍作成トリガー)
        if payload.channel_id != self.book_note_channel_id: return
        emoji_str = str(payload.emoji)
        if emoji_str == BOT_PROCESS_TRIGGER_REACTION:
            if payload.user_id != self.bot.user.id: return 
            channel = self.bot.get_channel(payload.channel_id)
            if not channel: return
            try: message = await channel.fetch_message(payload.message_id)
            except (discord.NotFound, discord.Forbidden): return
            is_processed = any(r.emoji in (PROCESS_START_EMOJI, PROCESS_COMPLETE_EMOJI, PROCESS_ERROR_EMOJI, API_ERROR_EMOJI, NOT_FOUND_EMOJI) and r.me for r in message.reactions)
            if is_processed: return
            logging.info(f"BookCog: Botの '{BOT_PROCESS_TRIGGER_REACTION}' を検知。書籍ノート作成処理を開始: {message.jump_url}")
            try: await message.remove_reaction(payload.emoji, self.bot.user)
            except discord.HTTPException: pass
            await self._create_book_note(message)

    async def _create_book_note(self, message: discord.Message):
        # (変更なし: 書籍作成ロジック)
        error_reactions = set()
        book_data = None
        source_url = message.content.strip()
        try:
            await message.add_reaction(PROCESS_START_EMOJI)
            logging.info(f"BookCog: Waiting 7s for Discord embed for {source_url}...")
            await asyncio.sleep(7)
            book_title = None
            try:
                fetched_message = await message.channel.fetch_message(message.id)
                if fetched_message.embeds and fetched_message.embeds[0].title:
                    book_title = fetched_message.embeds[0].title
            except (discord.NotFound, discord.Forbidden): pass
            if not book_title:
                error_reactions.add(PROCESS_ERROR_EMOJI)
                raise Exception("Discord Embedから書籍タイトルを取得できませんでした。")
            book_data = await self._fetch_google_book_data(book_title)
            if not book_data:
                error_reactions.add(NOT_FOUND_EMOJI)
                raise Exception("Google Books APIで書籍データが見つかりませんでした。")
            await self._save_note_to_obsidian(book_data, source_url)
            await message.add_reaction(PROCESS_COMPLETE_EMOJI)
        except Exception as e:
            logging.error(f"BookCog: 書籍ノート作成処理中にエラー: {e}", exc_info=True)
            if not error_reactions: error_reactions.add(PROCESS_ERROR_EMOJI)
            for reaction in error_reactions:
                try: await message.add_reaction(reaction)
                except discord.HTTPException: pass
        finally:
            try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except discord.HTTPException: pass

    async def _fetch_google_book_data(self, title: str) -> dict | None:
        # (変更なし)
        if not self.google_books_api_key or not self.session: return None
        query = urllib.parse.quote_plus(title)
        url = f"https://www.googleapis.com/books/v1/volumes?q={query}&key={self.google_books_api_key}&maxResults=1&langRestrict=ja"
        try:
            async with self.session.get(url, timeout=15) as response:
                if response.status != 200: return None
                data = await response.json()
                if data.get("totalItems", 0) > 0 and "items" in data:
                    return data["items"][0].get("volumeInfo")
                else: return None
        except Exception: return None

    async def _save_note_to_obsidian(self, book_data: dict, source_url: str):
        # (変更なし)
        title = book_data.get("title", "不明なタイトル")
        author_str = ", ".join(book_data.get("authors", []))
        published_date = book_data.get("publishedDate", "N/A")
        description = book_data.get("description", "N/A")
        thumbnail_url = book_data.get("imageLinks", {}).get("thumbnail", "")
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)
        if not safe_title: safe_title = "Untitled Book"
        now = datetime.datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        note_filename = f"{safe_title}.md"
        note_path = f"{self.dropbox_vault_path}{READING_NOTES_PATH}/{note_filename}"
        note_content = f"""---
title: "{title}"
authors: [{author_str}]
published: {published_date}
source: {source_url}
tags: [book]
status: "To Read"
created: {now.isoformat()}
cover: {thumbnail_url}
---
## 概要
{description}
## メモ

## アクション

"""
        try:
            await asyncio.to_thread(
                self.dbx.files_upload, note_content.encode('utf-8'), note_path, mode=WriteMode('add')
            )
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
            daily_note_content = ""
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                daily_note_content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    daily_note_content = f"# {date_str}\n"
                else: raise
            link_path = f"{READING_NOTES_PATH.lstrip('/')}/{note_filename.replace('.md', '')}"
            link_to_add = f"- [[{link_path}|{title}]]"
            section_header = "## Reading Notes"
            new_daily_content = update_section(daily_note_content, link_to_add, section_header)
            await asyncio.to_thread(
                self.dbx.files_upload, new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite')
            )
        except Exception as e:
            logging.error(f"BookCog: ノート保存/デイリーノート更新中エラー: {e}", exc_info=True)
            raise

    # --- /book_memo コマンド (★ 修正: BookSelectView に渡す context を変更) ---
    @app_commands.command(name="book_memo", description="読書ノートを選択してメモを追記します。")
    async def book_memo(self, interaction: discord.Interaction):
        if not self.is_ready:
            await interaction.response.send_message("読書ノート機能は現在利用できません。", ephemeral=True)
            return
        if interaction.channel_id != self.book_note_channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.book_note_channel_id}> でのみ利用できます。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            book_files, error = await self.get_book_list()
            if error:
                await interaction.followup.send(error, ephemeral=True)
                return

            options = [discord.SelectOption(label=entry.name[:-3][:100], value=entry.path_display) for entry in book_files[:25]]
            
            # ★ 修正: original_context に interaction を渡す
            view = BookSelectView(self, options, original_context=interaction, action_type="memo")
            await interaction.followup.send("どの書籍にメモを追記しますか？", view=view, ephemeral=True)

        except Exception as e:
            logging.error(f"BookCog: /book_memo コマンド処理中に予期せぬエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ コマンド処理中に予期せぬエラーが発生しました: {e}", ephemeral=True)

    # --- /book_status コマンド (★ 修正: BookSelectView に渡す context を変更) ---
    @app_commands.command(name="book_status", description="読書ノートのステータスを変更します。")
    async def book_status(self, interaction: discord.Interaction):
        if not self.is_ready:
            await interaction.response.send_message("読書ノート機能は現在利用できません。", ephemeral=True)
            return
        if interaction.channel_id != self.book_note_channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.book_note_channel_id}> でのみ利用できます。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            book_files, error = await self.get_book_list()
            if error:
                await interaction.followup.send(error, ephemeral=True)
                return

            options = [discord.SelectOption(label=entry.name[:-3][:100], value=entry.path_display) for entry in book_files[:25]]
            
            # ★ 修正: original_context に interaction を渡す
            view = BookSelectView(self, options, original_context=interaction, action_type="status")
            await interaction.followup.send("どの書籍のステータスを変更しますか？", view=view, ephemeral=True)

        except Exception as e:
            logging.error(f"BookCog: /book_status コマンド処理中に予期せぬエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ コマンド処理中に予期せぬエラーが発生しました: {e}", ephemeral=True)

    # --- 書籍一覧取得ヘルパー (変更なし) ---
    async def get_book_list(self) -> tuple[list[FileMetadata], str | None]:
        try:
            folder_path = f"{self.dropbox_vault_path}{READING_NOTES_PATH}"
            result = await asyncio.to_thread(self.dbx.files_list_folder, folder_path, recursive=False)
            book_files = [entry for entry in result.entries if isinstance(entry, FileMetadata) and entry.name.endswith('.md')]
            
            if not book_files:
                return [], f"Obsidian Vaultの `{folder_path}` フォルダに読書ノートが見つかりませんでした。"

            book_files.sort(key=lambda x: x.server_modified, reverse=True)
            return book_files, None
        
        except ApiError as e:
            logging.error(f"BookCog: 読書ノート一覧の取得中にApiError: {e}", exc_info=True)
            return [], f"❌ 読書ノート一覧の取得中にDropboxエラーが発生しました: {e}"


async def setup(bot: commands.Bot):
    """Cogセットアップ"""
    # ★ (10) 必要なキーのチェックを強化
    if int(os.getenv("BOOK_NOTE_CHANNEL_ID", 0)) == 0:
        logging.error("BookCog: BOOK_NOTE_CHANNEL_ID が設定されていません。Cogをロードしません。")
        return
    if not os.getenv("GOOGLE_BOOKS_API_KEY"):
        logging.error("BookCog: GOOGLE_BOOKS_API_KEY が設定されていません。Cogをロードしません。")
        return
    if not os.getenv("OPENAI_API_KEY"):
        logging.error("BookCog: OPENAI_API_KEY が設定されていません (音声メモ不可)。Cogをロードしません。")
        return
    if not os.getenv("GEMINI_API_KEY"):
        logging.error("BookCog: GEMINI_API_KEY が設定されていません (手書きメモ不可)。Cogをロードしません。")
        return
        
    cog_instance = BookCog(bot)
    if cog_instance.is_ready:
        await bot.add_cog(cog_instance)
        logging.info("BookCog loaded successfully.")
    else:
        logging.error("BookCog failed to initialize properly and was not loaded.")