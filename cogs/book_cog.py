import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import asyncio
import dropbox
# FileMetadataを追加
from dropbox.files import WriteMode, DownloadError, FileMetadata 
from dropbox.exceptions import ApiError
import datetime
import zoneinfo
import aiohttp
import urllib.parse
import openai # (1) 音声認識 (Whisper) のために追加
import google.generativeai as genai # ★ (Fix 1) 不足していたインポートを追加
from PIL import Image # (2) 画像処理のために追加
import io
import pathlib

# 共通関数をインポート
try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.warning("BookCog: utils/obsidian_utils.pyが見つからないため、簡易的な追記処理を使用します。")
    # 簡易的なダミー関数 (フォールバック)
    def update_section(current_content: str, text_to_add: str, section_header: str) -> str:
        # 簡易的な追記処理（元の関数の完全な再現ではない）
        if section_header in current_content:
            lines = current_content.split('\n')
            try:
                header_index = -1
                for i, line in enumerate(lines):
                    # 修正: 英語の見出しにも対応できるよう lower() を追加
                    if line.strip().lstrip('#').strip().lower() == section_header.lstrip('#').strip().lower():
                        header_index = i
                        break
                if header_index == -1: raise ValueError("Header not found")
                
                insert_index = header_index + 1
                while insert_index < len(lines) and not lines[insert_index].strip().startswith('## '):
                    insert_index += 1
                
                if insert_index > header_index + 1 and lines[insert_index - 1].strip() != "":
                    lines.insert(insert_index, "")
                    insert_index += 1
                    
                lines.insert(insert_index, text_to_add)
                return "\n".join(lines)
            except ValueError:
                 return f"{current_content.strip()}\n\n{section_header}\n{text_to_add}\n"
        else:
            return f"{current_content.strip()}\n\n{section_header}\n{text_to_add}\n"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
READING_NOTES_PATH = "/Reading Notes" # Obsidian Vault内の保存先

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

# (3) 対応するファイルタイプ (zero-second_thinking_cog.py から流用)
SUPPORTED_AUDIO_TYPES = [
    'audio/mpeg', 'audio/x-m4a', 'audio/ogg', 'audio/wav', 'audio/webm'
]
SUPPORTED_IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp']

# (4) HEIC/HEIF対応 (オプション)
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    SUPPORTED_IMAGE_TYPES.extend(['image/heic', 'image/heif'])
    logging.info("BookCog: HEIC/HEIF image support enabled.")
except ImportError:
    logging.warning("BookCog: pillow_heif not installed. HEIC/HEIF support is disabled.")


# --- ★ 修正: テキストメモ入力モーダル (Modal) ---
# (以前の /book_memo コマンドの機能)
class BookMemoModal(discord.ui.Modal, title="読書メモの入力"):
    memo_text = discord.ui.TextInput(
        label="書籍に関するメモを入力してください",
        style=discord.TextStyle.paragraph,
        placeholder="例: p.56 〇〇という視点は新しい...",
        required=True,
        max_length=1500
    )

    def __init__(self, cog, selected_book_path: str):
        super().__init__(timeout=1800) # 30分
        self.cog = cog
        self.book_path = selected_book_path

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        try:
            # 1. 既存のノートをダウンロード
            logging.info(f"BookCog: メモ追記のためノートをダウンロード: {self.book_path}")
            _, res = await asyncio.to_thread(self.cog.dbx.files_download, self.book_path)
            current_content = res.content.decode('utf-8')

            # 2. メモをフォーマット (日付と時刻)
            now = datetime.datetime.now(JST)
            date_time_str = now.strftime('%Y-%m-%d %H:%M') # 日付と時刻
            # 複数行入力に対応
            memo_lines = self.memo_text.value.strip().split('\n')
            formatted_memo = f"- {date_time_str}\n\t- " + "\n\t- ".join(memo_lines)

            # 3. update_section で追記 (英語の見出し)
            section_header = "## Notes"
            new_content = update_section(current_content, formatted_memo, section_header)

            # 4. Dropboxにアップロード
            await asyncio.to_thread(
                self.cog.dbx.files_upload,
                new_content.encode('utf-8'),
                self.book_path,
                mode=WriteMode('overwrite')
            )
            
            logging.info(f"BookCog: 読書メモを追記しました: {self.book_path}")
            await interaction.followup.send(f"✅ テキストメモを追記しました。\n`{os.path.basename(self.book_path)}`", ephemeral=True)

        except ApiError as e:
            logging.error(f"BookCog: 読書メモ追記中のDropbox APIエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ メモ追記中にDropboxエラーが発生しました: {e}", ephemeral=True)
        except Exception as e:
            logging.error(f"BookCog: 読書メモ追記中の予期せぬエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ メモ追記中に予期せぬエラーが発生しました: {e}", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logging.error(f"Error in BookMemoModal: {error}", exc_info=True)
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ モーダル処理中にエラーが発生しました: {error}", ephemeral=True)
        else:
            try:
                await interaction.response.send_message(f"❌ モーダル処理中にエラーが発生しました: {error}", ephemeral=True)
            except discord.InteractionResponded:
                pass

# --- ステータス変更用ボタンView ---
class BookStatusView(discord.ui.View):
    def __init__(self, cog, book_path: str, original_context: discord.Interaction | discord.Message):
        super().__init__(timeout=300) 
        self.cog = cog
        self.book_path = book_path
        self.original_context = original_context

    async def _delete_original_context(self):
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

# --- 書籍選択用ドロップダウン (汎用化) ---
class BookSelectView(discord.ui.View):
    def __init__(self, 
                 cog, 
                 book_options: list[discord.SelectOption], 
                 original_context: discord.Interaction | discord.Message, 
                 action_type: str, 
                 attachment: discord.Attachment = None, 
                 text_memo: str = None, 
                 input_type: str = None
                 ):
        super().__init__(timeout=600)
        self.cog = cog
        self.original_context = original_context
        self.action_type = action_type # "memo" (Modal), "status", "add_memo" (on_message)
        self.attachment = attachment
        self.text_memo = text_memo
        self.input_type = input_type # "audio", "image", or "text"
        
        placeholder_text = "操作対象の書籍を選択してください..."
        if action_type == "memo":
            placeholder_text = "メモを追記する書籍を選択 (コマンド)..."
        elif action_type == "status":
            placeholder_text = "ステータスを変更する書籍を選択..."
        elif action_type == "add_memo":
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
                # (Fix 4) 音声メッセージは編集できないため、HTTPException(code 50162)をキャッチ
                try:
                    await self.original_context.edit(**kwargs)
                except discord.HTTPException as e_msg:
                    if e_msg.code == 50162: # 50162 = Voice messages cannot be edited
                        logging.warning(f"BookSelectView: 元メッセージは音声メッセージのため編集をスキップ: {e_msg.text}")
                    else:
                        raise # 他のHTTPExceptionは再発生させる
        except discord.HTTPException as e:
            logging.warning(f"BookSelectView: 元のメッセージの編集に失敗: {e}")

    async def select_callback(self, interaction: discord.Interaction):
        selected_path = interaction.data["values"][0]
        
        if self.action_type == "memo": # /book_memo コマンド
            modal = BookMemoModal(self.cog, selected_path)
            await interaction.response.send_modal(modal)
            await self._edit_original_response(content="テキストメモを入力中です...", view=None)

        elif self.action_type == "status": # /book_status コマンド
            selected_option_label = next((opt.label for opt in interaction.message.components[0].children[0].options if opt.value == selected_path), "選択された書籍")
            status_view = BookStatusView(self.cog, selected_path, self.original_context)
            await interaction.response.edit_message(
                content=f"**{selected_option_label}** のステータスを選択してください:",
                view=status_view
            )

        elif self.action_type == "add_memo": # on_message (text, audio, image)
            # ★ 修正: 添付ファイル/テキストメモの処理を開始
            # (Fix 4) 編集を interaction.response.edit_message に変更
            await interaction.response.edit_message(
                content=f"`{os.path.basename(selected_path)}` に {self.input_type} メモを処理中です... {PROCESS_START_EMOJI}", 
                view=None
            )
            
            await self.cog.process_posted_memo(
                interaction, 
                self.original_context, # 元のファイル添付/テキストメッセージ
                selected_path, 
                self.input_type,
                self.attachment, # None if text
                self.text_memo   # None if attachment
            )
        
        self.stop()

    async def on_timeout(self):
        await self._edit_original_response(content="書籍の選択がタイムアウトしました。", view=None)


# --- 書籍「作成」の確認View (ドロップダウン) ---
class BookCreationSelectView(discord.ui.View):
    def __init__(self, cog, book_results: list[dict], source_url: str, embed_image_url_fallback: str | None, original_message: discord.Message):
        super().__init__(timeout=600) # 10分
        self.cog = cog
        self.book_results = book_results # Google APIからの結果リスト
        self.source_url = source_url
        self.embed_image_url_fallback = embed_image_url_fallback
        self.original_message = original_message
        self.confirmation_message = None # ボットが送信する確認メッセージ

        options = []
        for i, book_data in enumerate(book_results):
            if i >= 25: break # Selectの最大オプション数
            if not book_data: continue # API結果がNoneのものはスキップ
            
            title = book_data.get("title", "不明なタイトル")
            authors = ", ".join(book_data.get("authors", ["著者不明"]))
            
            # ラベルと説明を生成
            label = (title[:97] + '...') if len(title) > 100 else title
            description = (authors[:97] + '...') if len(authors) > 100 else authors
            
            options.append(discord.SelectOption(
                label=label,
                description=description,
                value=str(i) # 値はリストのインデックス
            ))
        
        if not options:
            options.append(discord.SelectOption(label="候補が見つかりませんでした", value="-1", default=True))

        select = discord.ui.Select(
            placeholder="検出された候補から正しい書籍を選択してください...",
            options=options,
            custom_id="book_creation_select"
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        selected_index_str = interaction.data["values"][0]
        
        if selected_index_str == "-1":
            await interaction.followup.send("有効な書籍が選択されませんでした。", ephemeral=True, delete_after=10)
            await self.confirmation_message.edit(content="処理がキャンセルされました。", embed=None, view=None)
            await self.original_message.add_reaction(PROCESS_ERROR_EMOJI)
            self.stop()
            return

        try:
            selected_index = int(selected_index_str)
            selected_book_data = self.book_results[selected_index]

            # ★ 修正: _save_note_to_obsidian を呼び出す
            save_result = await self.cog._save_note_to_obsidian(selected_book_data, self.source_url, self.embed_image_url_fallback)
            
            # ★ 修正: 戻り値(True/"EXISTS"/False)をチェック
            if save_result == True:
                # 元のメッセージに完了リアクション
                await self.original_message.add_reaction(PROCESS_COMPLETE_EMOJI)
                # 確認メッセージを編集して終了
                await self.confirmation_message.edit(content=f"✅ 読書ノート「{selected_book_data.get('title')}」を作成しました。", embed=None, view=None)
                await interaction.followup.send("ノートを作成しました。", ephemeral=True, delete_after=5)
            
            elif save_result == "EXISTS":
                book_name = selected_book_data.get("title", "選択された書籍")
                logging.warning(f"BookCog: ノート作成が重複しています: {book_name}")
                await interaction.followup.send(f"❌ ノート作成に失敗しました。\n`{book_name}` という名前のノートが既にObsidian Vaultに存在します。", ephemeral=True)
                await self.original_message.add_reaction(PROCESS_ERROR_EMOJI)
                await self.confirmation_message.edit(content="❌ 作成に失敗しました。ノートが既に存在します。", embed=None, view=None)
            
            else: # False (予期せぬエラー)
                raise Exception("_save_note_to_obsidianがFalseを返しました。")

        except (ValueError, IndexError) as e_idx:
            logging.error(f"BookCreationSelectView: 選択インデックスのエラー: {e_idx}", exc_info=True)
            await interaction.followup.send("❌ 選択処理中にエラーが発生しました。", ephemeral=True)
            await self.original_message.add_reaction(PROCESS_ERROR_EMOJI)
        except Exception as e:
            logging.error(f"BookCreationSelectView: ノート作成処理中にエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ ノート作成中にエラーが発生しました: {e}", ephemeral=True)
            await self.original_message.add_reaction(PROCESS_ERROR_EMOJI)
            if self.confirmation_message:
                await self.confirmation_message.edit(content=f"❌ エラーが発生しました: {e}", embed=None, view=None)
        finally:
            self.stop()
    
    async def on_timeout(self):
        try:
            if self.confirmation_message:
                await self.confirmation_message.edit(content="タイムアウトしました。処理はキャンセルされました。", embed=None, view=None)
            await self.original_message.add_reaction("⚠️")
        except discord.HTTPException:
            pass


class BookCog(commands.Cog):
    """Google Books APIと連携し、読書ノートを作成するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.book_note_channel_id = int(os.getenv("BOOK_NOTE_CHANNEL_ID", 0))
        self.google_books_api_key = os.getenv("GOOGLE_BOOKS_API_KEY")
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        
        self.dbx = None
        self.session = None
        self.is_ready = False
        
        if not all([self.book_note_channel_id, self.google_books_api_key, self.dropbox_refresh_token, self.openai_api_key, self.gemini_api_key]):
            logging.error("BookCog: 必要な環境変数 (BOOK_NOTE_CHANNEL_ID, GOOGLE_BOOKS_API_KEY, DROPBOX_REFRESH_TOKEN, OPENAI_API_KEY, GEMINI_API_KEY) が不足。Cogは動作しません。")
            return

        try:
            self.dbx = dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret, 
                timeout=60 # タイムアウトを60秒に短縮
            )
            self.dbx.users_get_current_account()
            logging.info("BookCog: Dropbox client initialized.")

            self.session = aiohttp.ClientSession()
            logging.info("BookCog: aiohttp session started.")
            
            self.openai_client = openai.AsyncOpenAI(api_key=self.openai_api_key)
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_vision_model = genai.GenerativeModel("gemini-2.5-pro") 
            
            self.is_ready = True

        except Exception as e:
            logging.error(f"BookCog: Failed to initialize clients: {e}", exc_info=True)

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()
            logging.info("BookCog: aiohttp session closed.")

    async def _update_book_status(self, book_path: str, new_status: str) -> bool:
        """指定されたノートのYAMLフロントマターのstatusを更新する"""
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
                else: return False
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

    # ★ --- 修正: on_message リスナー (テキストメモにも対応) ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """
        BOOK_NOTE_CHANNEL_ID に投稿されたメッセージ（テキスト・添付ファイル）を検知し、
        どの書籍ノートに追記するかをユーザーに尋ねる。
        """
        # --- 基本チェック ---
        if not self.is_ready or message.author.bot or message.channel.id != self.book_note_channel_id:
            return
        # スラッシュコマンドやリプライは無視 (リプライは on_raw_reaction_add で処理)
        # ★ 修正: 読書ノート作成トリガーのURL投稿も無視する
        if message.content.startswith('/') or message.reference or message.content.startswith('http'):
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
                logging.debug(f"BookCog: サポート対象外の添付ファイルタイプ: {attachment.content_type}")
                return # サポート対象外のファイル
        else:
            # 添付ファイルがない場合 (テキストメモ)
            text_memo = message.content.strip()
            if not text_memo:
                return # 空のメッセージは無視
            input_type = "text"
        
        logging.info(f"BookCog: {input_type} メモを検知: {message.jump_url}")
        
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
                action_type="add_memo", # ★ "add_memo" タイプに変更
                attachment=attachment, 
                text_memo=text_memo,
                input_type=input_type
            )
            await message.reply(f"この {input_type} メモはどの書籍のものですか？", view=view, mention_author=False)
            # 🤔 は消さない (ユーザーの選択待ち)

        except Exception as e:
            logging.error(f"BookCog: on_message での添付ファイル/テキスト処理中にエラー: {e}", exc_info=True)
            await message.reply(f"❌ メモの処理開始中にエラーが発生しました: {e}")
            try:
                await message.remove_reaction("🤔", self.bot.user)
                await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException:
                pass

    # ★ --- 修正: process_posted_memo (テキストメモにも対応) ---
    async def process_posted_memo(
        self, 
        interaction: discord.Interaction, # SelectViewからのInteraction
        original_message: discord.Message, # ユーザーが添付/投稿した元のMessage
        book_path: str, 
        input_type: str,
        attachment: discord.Attachment = None, # None if text
        text_memo: str = None # None if attachment
    ):
        """投稿されたメモ（テキスト、音声、画像）をテキスト化し、指定されたノートに追記する"""
        
        temp_audio_path = None
        recognized_text = ""
        
        try:
            # 元メッセージのリアクションを更新
            await original_message.remove_reaction("🤔", self.bot.user)
            await original_message.add_reaction(PROCESS_START_EMOJI)

            # 1. テキスト化 (入力タイプで分岐)
            if input_type == "text":
                recognized_text = text_memo
                logging.info(f"BookCog: テキストメモを処理: {recognized_text[:50]}...")

            elif input_type == "audio":
                async with self.session.get(attachment.url) as resp:
                    if resp.status != 200: raise Exception(f"ファイルダウンロード失敗: Status {resp.status}")
                    file_bytes = await resp.read()
                
                # (Fix 3) 拡張子を含めるように修正 (attachment.filename を使う)
                temp_audio_path = pathlib.Path(f"./temp_book_audio_{original_message.id}_{attachment.filename}")
                temp_audio_path.write_bytes(file_bytes)
                
                with open(temp_audio_path, "rb") as audio_file:
                    transcription = await self.openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file)
                recognized_text = transcription.text
                logging.info(f"BookCog: 音声認識完了 (Whisper): {recognized_text[:50]}...")

            elif input_type == "image":
                async with self.session.get(attachment.url) as resp:
                    if resp.status != 200: raise Exception(f"ファイルダウンロード失敗: Status {resp.status}")
                    file_bytes = await resp.read()
                    
                img = Image.open(io.BytesIO(file_bytes))
                vision_prompt = [
                    "この画像は手書きのメモです。内容を読み取り、箇条書きのMarkdown形式でテキスト化してください。返答には前置きや説明は含めず、箇条書きのテキスト本体のみを生成してください。",
                    img,
                ]
                response = await self.gemini_vision_model.generate_content_async(vision_prompt)
                recognized_text = response.text.strip()
                logging.info(f"BookCog: 手書きメモ認識完了 (Gemini): {recognized_text[:50]}...")

            if not recognized_text:
                raise Exception("AIによるテキスト化の結果が空か、入力タイプが不明でした。")

            # 2. ノートに追記
            _, res = await asyncio.to_thread(self.dbx.files_download, book_path)
            current_content = res.content.decode('utf-8')
            
            now = datetime.datetime.now(JST)
            date_time_str = now.strftime('%Y-%m-%d %H:%M')
            memo_lines = recognized_text.strip().split('\n')
            formatted_memo = f"- {date_time_str} ({input_type} memo)\n\t- " + "\n\t- ".join(memo_lines)
            
            section_header = "## Notes"
            new_content = update_section(current_content, formatted_memo, section_header)
            
            await asyncio.to_thread(
                self.dbx.files_upload, new_content.encode('utf-8'), book_path, mode=WriteMode('overwrite')
            )
            
            logging.info(f"BookCog: {input_type} メモを追記しました: {book_path}")
            # (Fix 4) defer 済みの interaction への応答は followup を使う
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
        """Botが付けた 📥 リアクションを検知して書籍作成プロセスを開始"""
        # ★ 修正: http(s):// で始まるメッセージも on_message で処理するため、
        # このリアクショントリガーは「http(s)://」で始まるメッセージに「のみ」反応するようにする
        if payload.channel_id != self.book_note_channel_id: return
        emoji_str = str(payload.emoji)
        
        if emoji_str == BOT_PROCESS_TRIGGER_REACTION:
            if payload.user_id != self.bot.user.id: return 
            
            channel = self.bot.get_channel(payload.channel_id)
            if not channel: return
            try: message = await channel.fetch_message(payload.message_id)
            except (discord.NotFound, discord.Forbidden): return
            
            # ★ 修正: URLで始まるメッセージでなければ、このトリガーは無視
            if not message.content.strip().startswith('http'):
                return

            is_processed = any(r.emoji in (PROCESS_START_EMOJI, PROCESS_COMPLETE_EMOJI, PROCESS_ERROR_EMOJI, API_ERROR_EMOJI, NOT_FOUND_EMOJI) and r.me for r in message.reactions)
            if is_processed: return
            
            logging.info(f"BookCog: Botの '{BOT_PROCESS_TRIGGER_REACTION}' を検知。書籍ノート作成処理を開始: {message.jump_url}")
            try: await message.remove_reaction(payload.emoji, self.bot.user)
            except discord.HTTPException: pass
            
            await self._start_book_selection_workflow(message)

    # _start_book_selection_workflow (書籍ノート作成の「選択」ワークフロー)
    async def _start_book_selection_workflow(self, message: discord.Message):
        """書籍ノート作成の「選択」ワークフローを開始する"""
        error_reactions = set()
        source_url = message.content.strip()

        try:
            await message.add_reaction(PROCESS_START_EMOJI)

            # 1. Embedから書籍タイトルと画像を取得 (7秒待機)
            logging.info(f"BookCog: Waiting 7s for Discord embed for {source_url}...")
            await asyncio.sleep(7)
            
            book_title = None
            embed_image_url = None
            
            try:
                fetched_message = await message.channel.fetch_message(message.id)
                if fetched_message.embeds:
                    embed = fetched_message.embeds[0]
                    if embed.title: book_title = embed.title
                    if embed.thumbnail and embed.thumbnail.url: embed_image_url = embed.thumbnail.url
                    elif embed.image and embed.image.url: embed_image_url = embed.image.url
            except (discord.NotFound, discord.Forbidden) as e:
                 logging.warning(f"BookCog: Embed取得のためのメッセージ再取得に失敗: {e}")

            if not book_title:
                logging.error(f"BookCog: Discord Embedから書籍タイトルを取得できませんでした。URL: {source_url}")
                error_reactions.add(PROCESS_ERROR_EMOJI)
                raise Exception("Discord Embedから書籍タイトルを取得できませんでした。")

            # 2. Google Books APIで書籍データを「複数」検索
            book_results = await self._fetch_google_book_data(book_title)
            
            if not book_results:
                logging.warning(f"BookCog: Google Books APIで「{book_title}」の候補が見つかりませんでした。")
                error_reactions.add(NOT_FOUND_EMOJI)
                raise Exception("Google Books APIで書籍データが見つかりませんでした。")

            logging.info(f"BookCog: Google Books APIで {len(book_results)} 件の候補を取得しました。")

            # 3. 選択肢Viewを送信
            view = BookCreationSelectView(
                self, 
                book_results, 
                source_url, 
                embed_image_url, 
                message
            )
            
            confirm_msg = await message.reply(
                "Google Books APIで以下の候補が見つかりました。\n作成したい書籍を選択してください (10分以内)。", 
                view=view, 
                mention_author=False
            )
            view.confirmation_message = confirm_msg # Viewに確認メッセージ自身を渡す

            # 元メッセージの ⏳ はViewが表示されたら消す
            await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)

        except Exception as e:
            logging.error(f"BookCog: 書籍ノート作成処理中にエラー: {e}", exc_info=True)
            if not error_reactions:
                error_reactions.add(PROCESS_ERROR_EMOJI)
            for reaction in error_reactions:
                try: await message.add_reaction(reaction)
                except discord.HTTPException: pass
            try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except discord.HTTPException: pass

    # _fetch_google_book_data (maxResults=5, 戻り値をlist[dict]に)
    async def _fetch_google_book_data(self, title: str) -> list[dict] | None:
        """Google Books API v1 を叩いて書籍情報を取得する"""
        if not self.google_books_api_key or not self.session:
            logging.error("BookCog: Google Books APIキーまたはaiohttpセッションがありません。")
            return None
            
        query = urllib.parse.quote_plus(title)
        # maxResults=5 に変更
        url = f"https://www.googleapis.com/books/v1/volumes?q={query}&key={self.google_books_api_key}&maxResults=5&langRestrict=ja"
        
        try:
            async with self.session.get(url, timeout=15) as response:
                if response.status != 200:
                    logging.error(f"Google Books API Error: Status {response.status}, Response: {await response.text()}")
                    return None
                
                data = await response.json()
                
                if data.get("totalItems", 0) > 0 and "items" in data:
                    # volumeInfo のリストを返す
                    return [item.get("volumeInfo") for item in data["items"] if item.get("volumeInfo")]
                else:
                    return None
        except asyncio.TimeoutError:
            logging.error(f"Google Books API request timed out for title: {title}")
            return None
        except aiohttp.ClientError as e:
            logging.error(f"Google Books API client error for title {title}: {e}", exc_info=True)
            return None

    # ★ --- 修正: _save_note_to_obsidian (競合チェック機能の追加) ---
    async def _save_note_to_obsidian(self, book_data: dict, source_url: str, embed_image_url_fallback: str = None) -> bool | str:
        """
        取得した書籍データからMarkdownノートを作成し、Dropboxに保存する。
        成功時は True、競合時は "EXISTS"、失敗時は False を返す。
        """
        
        title = book_data.get("title", "不明なタイトル")
        authors = book_data.get("authors", [])
        author_str = ", ".join(authors)
        published_date = book_data.get("publishedDate", "N/A")
        description = book_data.get("description", "N/A")
        
        thumbnail_url = book_data.get("imageLinks", {}).get("thumbnail", "")
        if not thumbnail_url and embed_image_url_fallback:
            thumbnail_url = embed_image_url_fallback
            logging.info("BookCog: Google Books APIの画像が見つからないため、Discord Embedの画像URLをカバーとして使用します。")
        elif not thumbnail_url:
            logging.warning("BookCog: Google Books APIにもDiscord Embedにもカバー画像が見つかりませんでした。")
        
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)
        if not safe_title: safe_title = "Untitled Book"
        
        now = datetime.datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        note_filename = f"{safe_title}.md"
        note_path = f"{self.dropbox_vault_path}{READING_NOTES_PATH}/{note_filename}"

        # --- ★ 修正: 競合チェック ---
        try:
            # 既存のファイル一覧を取得 (軽量なメタデータのみ)
            existing_files, _ = await self.get_book_list(check_only=True)
            existing_paths = [entry.path_display.lower() for entry in existing_files] # 比較用に小文字化

            if note_path.lower() in existing_paths:
                logging.warning(f"BookCog: ノート作成が競合しました。ファイルが既に存在します: {note_path}")
                return "EXISTS" # 競合を通知
        except Exception as e_check:
            # チェックに失敗した場合は、エラーとして処理を中断
            logging.error(f"BookCog: 既存ノートのチェック中にエラー: {e_check}", exc_info=True)
            return False
        # --- 競合チェックここまで ---

        # --- ノートテンプレート (見出しを英語に) ---
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
## Summary
{description}

## Notes

## Actions

"""
        # --- ここまで ---

        try:
            # ★ 修正: mode='add' を維持 (チェックを通過したので競合しないはず)
            await asyncio.to_thread(
                self.dbx.files_upload, note_content.encode('utf-8'), note_path, mode=WriteMode('add')
            )
            logging.info(f"BookCog: 読書ノートを保存しました: {note_path}")
            
            # --- デイリーノートにリンクを追記 ---
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
            logging.info(f"BookCog: デイリーノートに読書ノートへのリンクを追記しました: {daily_note_path}")
            return True # 成功

        except ApiError as e:
            # mode='add' での UploadError(conflict) は、チェックをすり抜けた競合
            if isinstance(e.error, dropbox.files.UploadError) and e.error.is_path() and e.error.get_path().is_conflict():
                 logging.warning(f"BookCog: ノート保存が競合しました (チェックすり抜け): {note_path}")
                 return "EXISTS"
            logging.error(f"BookCog: Dropboxへのノート保存またはデイリーノート更新中にApiError: {e}", exc_info=True)
            return False # その他のエラー
        except Exception as e:
            logging.error(f"BookCog: ノート保存またはデイリーノート更新中に予期せぬエラー: {e}", exc_info=True)
            return False

    # --- ★ 修正: /book_memo コマンド (Modalを呼び出すように変更) ---
    @app_commands.command(name="book_memo", description="読書ノートを選択してテキストメモを追記します。")
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
            
            # action_type="memo" (モーダル呼び出し) を渡す
            view = BookSelectView(self, options, original_context=interaction, action_type="memo")
            await interaction.followup.send("どの書籍にメモを追記しますか？", view=view, ephemeral=True)

        except Exception as e:
            logging.error(f"BookCog: /book_memo コマンド処理中に予期せぬエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ コマンド処理中に予期せぬエラーが発生しました: {e}", ephemeral=True)

    # --- /book_status コマンド (修正) ---
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
            
            # original_context に interaction を渡す
            view = BookSelectView(self, options, original_context=interaction, action_type="status")
            await interaction.followup.send("どの書籍のステータスを変更しますか？", view=view, ephemeral=True)

        except Exception as e:
            logging.error(f"BookCog: /book_status コマンド処理中に予期せぬエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ コマンド処理中に予期せぬエラーが発生しました: {e}", ephemeral=True)

    # --- ★ 修正: 書籍一覧取得ヘルパー (チェック専用フラグ追加) ---
    async def get_book_list(self, check_only: bool = False) -> tuple[list[FileMetadata], str | None]:
        """Dropboxから書籍ノートの一覧を取得する共通ヘルパー"""
        try:
            folder_path = f"{self.dropbox_vault_path}{READING_NOTES_PATH}"
            # ★ 修正: フォルダが存在しない場合を考慮
            try:
                result = await asyncio.to_thread(self.dbx.files_list_folder, folder_path, recursive=False)
            except ApiError as e:
                if isinstance(e.error, dropbox.files.ListFolderError) and e.error.is_path() and e.error.get_path().is_not_found():
                    logging.warning(f"BookCog: 読書ノートフォルダが見つかりません: {folder_path}")
                    return [], f"Obsidian Vaultの `{folder_path}` フォルダが見つかりませんでした。"
                else:
                    raise # 他のAPIエラーは再発生させる
            
            book_files = []
            for entry in result.entries:
                if isinstance(entry, FileMetadata) and entry.name.endswith('.md'):
                    book_files.append(entry)
            
            # 競合チェックのみが目的の場合は、ソートや空リストのチェックは不要
            if check_only:
                return book_files, None

            if not book_files:
                return [], f"Obsidian Vaultの `{folder_path}` フォルダに読書ノートが見つかりませんでした。"

            # 最終更新日時でソート (新しいものが上)
            book_files.sort(key=lambda x: x.server_modified, reverse=True)
            return book_files, None
        
        except ApiError as e:
            logging.error(f"BookCog: 読書ノート一覧の取得中にApiError: {e}", exc_info=True)
            return [], f"❌ 読書ノート一覧の取得中にDropboxエラーが発生しました: {e}"


async def setup(bot: commands.Bot):
    """Cogセットアップ"""
    # (10) 必要なキーのチェックを強化
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