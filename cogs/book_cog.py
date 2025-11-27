import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import asyncio
import dropbox
from dropbox.files import WriteMode, DownloadError, FileMetadata 
from dropbox.exceptions import ApiError, AuthError
import datetime
import zoneinfo
import aiohttp
import urllib.parse
import openai
import google.generativeai as genai
from PIL import Image
import io
import pathlib
import json
import random

# 共通関数をインポート
try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.warning("BookCog: utils/obsidian_utils.pyが見つからないため、簡易的な追記処理を使用します。")
    def update_section(current_content: str, text_to_add: str, section_header: str) -> str:
        return f"{current_content.strip()}\n\n{section_header}\n{text_to_add}\n"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
READING_NOTES_PATH = "/Reading Notes"
BOOK_INDEX_PATH = f"{os.getenv('DROPBOX_VAULT_PATH', '/ObsidianVault')}/.bot/book_index.json"

# --- リアクション定数 ---
BOT_PROCESS_TRIGGER_REACTION = '📥' 
PROCESS_START_EMOJI = '⏳'
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'
API_ERROR_EMOJI = '☁️'
NOT_FOUND_EMOJI = '🧐'

STATUS_OPTIONS = {
    "wishlist": "Wishlist",
    "to_read": "To Read",
    "reading": "Reading",
    "finished": "Finished"
}

SUPPORTED_AUDIO_TYPES = [
    'audio/mpeg', 'audio/x-m4a', 'audio/ogg', 'audio/wav', 'audio/webm'
]
SUPPORTED_IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp']

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    SUPPORTED_IMAGE_TYPES.extend(['image/heic', 'image/heif'])
    logging.info("BookCog: HEIC/HEIF image support enabled.")
except ImportError:
    logging.warning("BookCog: pillow_heif not installed. HEIC/HEIF support is disabled.")

# ==============================================================================
# === UI Components for Book List/Browsing =====================================
# ==============================================================================

class BookDetailView(discord.ui.View):
    def __init__(self, book_data):
        super().__init__(timeout=None)
        if book_data.get('source'):
            self.add_item(discord.ui.Button(label="元リンクを開く", url=book_data['source']))

class BookListView(discord.ui.View):
    def __init__(self, cog, books, page=0):
        super().__init__(timeout=180)
        self.cog = cog
        self.books = books
        self.page = page
        self.items_per_page = 10
        self.total_pages = (len(books) - 1) // self.items_per_page + 1
        self.update_buttons()

    def update_buttons(self):
        self.prev_button.disabled = (self.page == 0)
        self.next_button.disabled = (self.page >= self.total_pages - 1)
        self.page_count_button.label = f"Page {self.page + 1}/{self.total_pages}"
        
        # セレクトメニューの更新
        start = self.page * self.items_per_page
        end = start + self.items_per_page
        current_items = self.books[start:end]
        
        options = []
        for i, book in enumerate(current_items):
            label = book.get('title', '無題')[:90]
            authors = ", ".join(book.get('authors', []))[:90]
            status_emoji = "📖"
            if book.get('status') == STATUS_OPTIONS['finished']: status_emoji = "✅"
            elif book.get('status') == STATUS_OPTIONS['wishlist']: status_emoji = "✨"
            
            options.append(discord.SelectOption(
                label=f"{start + i + 1}. {label}",
                description=f"{status_emoji} {book.get('status', 'Unknown')} | {authors}",
                value=str(start + i)
            ))
        
        for item in self.children:
            if isinstance(item, discord.ui.Select):
                self.remove_item(item)
        
        if options:
            select = discord.ui.Select(placeholder="書籍を選択して詳細を表示...", options=options, row=1)
            select.callback = self.select_callback
            self.add_item(select)

    async def update_message(self, interaction: discord.Interaction):
        self.update_buttons()
        embed = await self.cog.create_book_list_embed(self.books, self.page)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀️ 前へ", style=discord.ButtonStyle.primary, row=0)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            await self.update_message(interaction)

    @discord.ui.button(label="Page 1/1", style=discord.ButtonStyle.secondary, disabled=True, row=0)
    async def page_count_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(label="次へ ▶️", style=discord.ButtonStyle.primary, row=0)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.total_pages - 1:
            self.page += 1
            await self.update_message(interaction)

    async def select_callback(self, interaction: discord.Interaction):
        index = int(interaction.data["values"][0])
        if 0 <= index < len(self.books):
            book = self.books[index]
            embed = self.cog.create_book_detail_embed(book)
            await interaction.response.send_message(embed=embed, view=BookDetailView(book), ephemeral=True)

# ==============================================================================
# === Existing UI Components (Memo, Status, Creation) ==========================
# ==============================================================================

class BookMemoEditModal(discord.ui.Modal, title="読書メモの編集"):
    memo_text = discord.ui.TextInput(
        label="認識されたテキスト（編集してください）",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1500
    )

    def __init__(self, cog, book_path: str, initial_text: str, original_message: discord.Message, confirmation_message: discord.Message, input_type: str):
        super().__init__(timeout=1800)
        self.cog = cog
        self.book_path = book_path
        self.memo_text.default = initial_text
        self.original_message = original_message
        self.confirmation_message = confirmation_message
        self.input_type = input_type

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except: return
             
        edited_text = self.memo_text.value
        try:
            await self.cog._save_memo_to_obsidian_and_cleanup(
                interaction,
                self.book_path,
                edited_text,
                self.input_type,
                self.original_message,
                self.confirmation_message
            )
            # メモ保存通知は一時的でOK（別途メッセージが更新されるため）
            await interaction.followup.send("✅ 編集されたメモを保存しました。", ephemeral=True, delete_after=10)
        except Exception as e:
            logging.error(f"BookCog: Error saving edited memo: {e}")
            await interaction.followup.send(f"❌ エラーが発生しました: {e}", ephemeral=True)

class BookManualInputModal(discord.ui.Modal, title="書籍情報の手動入力"):
    title_input = discord.ui.TextInput(label="書籍のタイトル", style=discord.TextStyle.short, required=True, max_length=200)
    authors_input = discord.ui.TextInput(label="著者名（カンマ区切り）", style=discord.TextStyle.short, required=False)
    published_input = discord.ui.TextInput(label="出版日", style=discord.TextStyle.short, required=False, placeholder="2024-01-01")
    cover_url_input = discord.ui.TextInput(label="カバー画像URL", style=discord.TextStyle.short, required=False)
    description_input = discord.ui.TextInput(label="概要", style=discord.TextStyle.paragraph, required=False, max_length=1000)

    def __init__(self, cog, source_url: str, original_message: discord.Message, confirmation_message: discord.Message):
        super().__init__(timeout=1800)
        self.cog = cog
        self.source_url = source_url
        self.original_message = original_message
        self.confirmation_message = confirmation_message

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        book_data = {
            "title": self.title_input.value,
            "authors": [a.strip() for a in self.authors_input.value.split(',')] if self.authors_input.value else ["著者不明"],
            "publishedDate": self.published_input.value or "N/A",
            "description": self.description_input.value or "N/A",
            "imageLinks": {"thumbnail": self.cover_url_input.value} if self.cover_url_input.value else {}
        }

        try:
            save_result = await self.cog._save_note_to_obsidian(book_data, self.source_url)
            if save_result == True:
                try: await self.confirmation_message.delete()
                except: pass
                # 通知は自動削除
                await interaction.followup.send(f"✅ ノート「{book_data['title']}」を作成しました。", ephemeral=True, delete_after=10)
            elif save_result == "EXISTS":
                await interaction.followup.send(f"⚠️ そのノートは既に存在します。", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)

class ConfirmMemoView(discord.ui.View):
    def __init__(self, cog, book_path: str, recognized_text: str, original_message: discord.Message, input_type: str):
        super().__init__(timeout=1800)
        self.cog = cog
        self.book_path = book_path
        self.recognized_text = recognized_text
        self.original_message = original_message
        self.input_type = input_type
        self.confirmation_message = None

    @discord.ui.button(label="✅ このまま保存", style=discord.ButtonStyle.success, custom_id="confirm_memo_save")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.cog._save_memo_to_obsidian_and_cleanup(
                interaction,
                self.book_path,
                self.recognized_text,
                self.input_type,
                self.original_message,
                self.confirmation_message
            )
            # 通知は自動削除
            await interaction.followup.send("✅ メモを保存しました。", ephemeral=True, delete_after=10)
        except Exception as e:
            await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)
        finally:
            self.stop()

    @discord.ui.button(label="✏️ 編集する", style=discord.ButtonStyle.primary, custom_id="edit_memo")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = BookMemoEditModal(
            self.cog, self.book_path, self.recognized_text, 
            self.original_message, self.confirmation_message, self.input_type
        )
        await interaction.response.send_modal(modal)
        self.stop()

class BookStatusView(discord.ui.View):
    def __init__(self, cog, book_path: str, original_context: discord.Interaction | discord.Message, current_status: str):
        super().__init__(timeout=300) 
        self.cog = cog
        self.book_path = book_path
        self.original_context = original_context
        self.current_status = current_status
        self.add_status_buttons()

    def add_status_buttons(self):
        for key, label in STATUS_OPTIONS.items():
            is_current = (label.lower() == self.current_status.lower())
            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary if is_current else discord.ButtonStyle.primary,
                custom_id=f"status_{key}",
                disabled=is_current
            )
            button.callback = self.handle_status_change
            self.add_item(button)

    async def handle_status_change(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        custom_id = interaction.data["custom_id"]
        new_status = STATUS_OPTIONS.get(custom_id.replace("status_", ""))
        
        if await self.cog._update_book_status(self.book_path, new_status):
            book_name = os.path.basename(self.book_path).replace(".md", "")
            # 通知は自動削除
            await interaction.followup.send(f"✅ ステータス変更: `{book_name}` -> **{new_status}**", ephemeral=True, delete_after=10)
            try:
                if isinstance(self.original_context, discord.Interaction):
                    await self.original_context.delete_original_response()
                elif isinstance(self.original_context, discord.Message):
                    await self.original_context.delete()
            except: pass
        else:
            await interaction.followup.send(f"❌ 変更に失敗しました。", ephemeral=True)
        self.stop()

class BookCreationSelectView(discord.ui.View):
    def __init__(self, cog, book_results: list[dict], source_url: str, embed_image_url_fallback: str | None, original_message: discord.Message):
        super().__init__(timeout=600)
        self.cog = cog
        self.book_results = book_results
        self.source_url = source_url
        self.embed_image_url_fallback = embed_image_url_fallback
        self.original_message = original_message
        self.confirmation_message = None

        options = []
        for i, book in enumerate(book_results[:25]):
            label = book.get("title", "無題")[:95]
            desc = ", ".join(book.get("authors", ["著者不明"]))[:95]
            options.append(discord.SelectOption(label=label, description=desc, value=str(i)))
        
        if not options:
            options.append(discord.SelectOption(label="候補なし", value="-1", default=True))

        select = discord.ui.Select(placeholder="書籍を選択...", options=options, custom_id="book_creation_select")
        select.callback = self.select_callback
        self.add_item(select)

        manual_btn = discord.ui.Button(label="手動入力", style=discord.ButtonStyle.primary, custom_id="manual_input", row=1)
        manual_btn.callback = self.manual_input_callback
        self.add_item(manual_btn)

    async def select_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        idx = int(interaction.data["values"][0])
        if idx == -1: return

        book_data = self.book_results[idx]
        result = await self.cog._save_note_to_obsidian(book_data, self.source_url, self.embed_image_url_fallback)
        
        if result == True:
            try: await self.confirmation_message.delete()
            except: pass
            # 通知は自動削除
            await interaction.followup.send(f"✅ 読書ノート「{book_data['title']}」を作成しました。", ephemeral=True, delete_after=10)
        elif result == "EXISTS":
            await interaction.followup.send(f"⚠️ そのノートは既に存在します。", ephemeral=True)
        else:
            await interaction.followup.send("❌ 作成に失敗しました。", ephemeral=True)
        self.stop()

    async def manual_input_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BookManualInputModal(self.cog, self.source_url, self.original_message, self.confirmation_message))
        self.stop()

class BookSelectView(discord.ui.View):
    def __init__(self, cog, book_options, original_context, action_type, attachment=None, text_memo=None, input_type=None):
        super().__init__(timeout=600)
        self.cog = cog
        self.original_context = original_context
        self.action_type = action_type
        self.attachment = attachment
        self.text_memo = text_memo
        self.input_type = input_type
        
        select = discord.ui.Select(placeholder="書籍を選択...", options=book_options)
        select.callback = self.select_callback
        self.add_item(select)
        self.bot_reply_message = None

    async def select_callback(self, interaction: discord.Interaction):
        selected_path = interaction.data["values"][0]
        
        if self.action_type == "status":
            await interaction.response.defer(ephemeral=True)
            current_status = await self.cog._get_current_status(selected_path)
            view = BookStatusView(self.cog, selected_path, self.original_context, current_status)
            await self.original_context.edit_original_response(content=f"ステータス変更: **{os.path.basename(selected_path)}**", view=view)
            
        elif self.action_type == "add_memo":
            # 選択完了したら選択メニューは消す
            try:
                if self.bot_reply_message: await self.bot_reply_message.delete()
            except: pass
            
            await self.cog.process_posted_memo(
                interaction, self.original_context, selected_path, self.input_type, 
                self.attachment, self.text_memo
            )
        self.stop()

# ==============================================================================
# === BookCog ==================================================================
# ==============================================================================

class BookCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = int(os.getenv("BOOK_NOTE_CHANNEL_ID", 0))
        self.google_books_api_key = os.getenv("GOOGLE_BOOKS_API_KEY")
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        
        self.dbx = None
        self.session = None
        self.is_ready = False
        
        if all([self.channel_id, self.google_books_api_key, self.dropbox_refresh_token, self.openai_api_key, self.gemini_api_key]):
            try:
                self.dbx = dropbox.Dropbox(
                    oauth2_refresh_token=self.dropbox_refresh_token,
                    app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret
                )
                self.session = aiohttp.ClientSession()
                self.openai_client = openai.AsyncOpenAI(api_key=self.openai_api_key)
                genai.configure(api_key=self.gemini_api_key)
                self.gemini_vision_model = genai.GenerativeModel("gemini-2.5-pro")
                self.is_ready = True
                logging.info("BookCog initialized.")
            except Exception as e:
                logging.error(f"BookCog init failed: {e}")

    async def cog_unload(self):
        if self.session: await self.session.close()

    # --- Browsing Feature ---

    @app_commands.command(name="books", description="保存済みの読書ノート一覧を表示します。")
    @app_commands.describe(status="ステータスで絞り込み", query="タイトルや著者名で検索")
    @app_commands.choices(status=[
        app_commands.Choice(name="Reading (読書中)", value="Reading"),
        app_commands.Choice(name="To Read (未読/積読)", value="To Read"),
        app_commands.Choice(name="Finished (読了)", value="Finished"),
        app_commands.Choice(name="Wishlist (欲しい)", value="Wishlist"),
    ])
    async def books_command(self, interaction: discord.Interaction, status: str = None, query: str = None):
        await interaction.response.defer(ephemeral=True)
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, BOOK_INDEX_PATH)
            all_books = json.loads(res.content.decode('utf-8'))
        except (ApiError, json.JSONDecodeError):
            await interaction.followup.send("📚 書籍データの読み込みに失敗しました（まだデータがない可能性があります）。", ephemeral=True)
            return

        filtered_books = all_books
        if status:
            filtered_books = [b for b in filtered_books if b.get('status') == status]
        if query:
            q = query.lower()
            filtered_books = [b for b in filtered_books if q in b['title'].lower() or any(q in a.lower() for a in b.get('authors', []))]

        if not filtered_books:
            await interaction.followup.send("📚 条件に一致する書籍は見つかりませんでした。", ephemeral=True)
            return

        view = BookListView(self, filtered_books)
        embed = await self.create_book_list_embed(filtered_books, 0)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def create_book_list_embed(self, books, page):
        start = page * 10
        current_items = books[start:start+10]
        total_pages = (len(books) - 1) // 10 + 1

        embed = discord.Embed(title="📚 読書ノート一覧", color=discord.Color.blue())
        desc = ""
        for i, book in enumerate(current_items):
            status_emoji = "📖"
            if book.get('status') == "Finished": status_emoji = "✅"
            elif book.get('status') == "Wishlist": status_emoji = "✨"
            
            desc += f"**{start+i+1}. {book['title']}**\n"
            desc += f"{status_emoji} {book.get('status')} | ✍️ {', '.join(book.get('authors', []))[:30]}\n\n"
        
        embed.description = desc
        embed.set_footer(text=f"Page {page + 1}/{total_pages} | Total {len(books)} books")
        return embed

    def create_book_detail_embed(self, book):
        embed = discord.Embed(title=f"📘 {book['title']}", color=discord.Color.green())
        if book.get('cover'): embed.set_thumbnail(url=book['cover'])
        
        authors = ", ".join(book.get('authors', []))
        embed.add_field(name="著者", value=authors, inline=True)
        embed.add_field(name="ステータス", value=book.get('status', 'Unknown'), inline=True)
        embed.add_field(name="登録日", value=book.get('added_at', '')[:10], inline=False)
        return embed

    # --- Index Management ---

    async def _update_book_index(self, book_data: dict, filename: str):
        try:
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, BOOK_INDEX_PATH)
                index = json.loads(res.content.decode('utf-8'))
            except: index = []

            # 重複削除
            index = [b for b in index if b['filename'] != filename]
            
            new_entry = {
                "title": book_data['title'],
                "authors": book_data.get('authors', []),
                "filename": filename,
                "status": book_data.get('status', STATUS_OPTIONS['to_read']),
                "cover": book_data.get('imageLinks', {}).get('thumbnail', ''),
                "source": book_data.get('infoLink', ''), 
                "added_at": datetime.datetime.now(JST).isoformat()
            }
            index.insert(0, new_entry)

            await asyncio.to_thread(
                self.dbx.files_upload,
                json.dumps(index, ensure_ascii=False, indent=2).encode('utf-8'),
                BOOK_INDEX_PATH,
                mode=WriteMode('overwrite')
            )
        except Exception as e:
            logging.error(f"Failed to update book index: {e}")

    # --- Note Creation & Memo ---

    async def _save_note_to_obsidian(self, book_data: dict, source_url: str, embed_image_url_fallback: str = None) -> bool | str:
        title = book_data.get("title", "Untitled")
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)
        filename = f"{safe_title}.md"
        path = f"{self.dropbox_vault_path}{READING_NOTES_PATH}/{filename}"

        try:
            # 重複チェック
            try: 
                self.dbx.files_get_metadata(path)
                return "EXISTS"
            except: pass

            thumbnail = book_data.get("imageLinks", {}).get("thumbnail", embed_image_url_fallback or "")
            now = datetime.datetime.now(JST)
            
            content = f"""---
title: "{title}"
authors: {json.dumps(book_data.get('authors', []), ensure_ascii=False)}
published: {book_data.get('publishedDate', 'N/A')}
source: {source_url}
tags: [book]
status: "{STATUS_OPTIONS['to_read']}"
created: {now.isoformat()}
cover: "{thumbnail}"
---
## Summary
{book_data.get('description', 'N/A')}

## Notes

## Actions
"""
            await asyncio.to_thread(self.dbx.files_upload, content.encode('utf-8'), path, mode=WriteMode('add'))
            
            # インデックス更新
            book_data['status'] = STATUS_OPTIONS['to_read']
            book_data['imageLinks'] = {'thumbnail': thumbnail}
            book_data['infoLink'] = source_url
            await self._update_book_index(book_data, filename)
            
            # デイリーノート更新 (省略)
            return True
        except Exception as e:
            logging.error(f"Save note error: {e}")
            return False

    async def _update_book_status(self, book_path: str, new_status: str) -> bool:
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, book_path)
            content = res.content.decode('utf-8')
            
            # YAMLフロントマターのstatusを更新
            new_content = re.sub(r'status: ".*?"', f'status: "{new_status}"', content, count=1)
            
            await asyncio.to_thread(self.dbx.files_upload, new_content.encode('utf-8'), book_path, mode=WriteMode('overwrite'))
            
            # インデックスも更新
            filename = os.path.basename(book_path)
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, BOOK_INDEX_PATH)
                index = json.loads(res.content.decode('utf-8'))
                for book in index:
                    if book['filename'] == filename:
                        book['status'] = new_status
                        break
                await asyncio.to_thread(self.dbx.files_upload, json.dumps(index, ensure_ascii=False, indent=2).encode('utf-8'), BOOK_INDEX_PATH, mode=WriteMode('overwrite'))
            except: pass
            
            return True
        except Exception as e:
            logging.error(f"Update status error: {e}")
            return False

    async def _get_current_status(self, book_path: str) -> str:
        # インデックスから高速に取得
        filename = os.path.basename(book_path)
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, BOOK_INDEX_PATH)
            index = json.loads(res.content.decode('utf-8'))
            for book in index:
                if book['filename'] == filename:
                    return book.get('status', STATUS_OPTIONS['to_read'])
        except: pass
        
        # フォールバック: ファイル読み込み
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, book_path)
            content = res.content.decode('utf-8')
            match = re.search(r'status: "(.*?)"', content)
            return match.group(1) if match else STATUS_OPTIONS['to_read']
        except: return "Unknown"

    # --- Message Handlers ---

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.is_ready or message.author.bot or message.channel.id != self.channel_id: return
        if message.content.startswith('/'): return

        # URLなら新規作成フロー
        if "http" in message.content:
            try:
                if not any(str(r.emoji) == BOT_PROCESS_TRIGGER_REACTION and r.me for r in message.reactions):
                    await message.add_reaction(BOT_PROCESS_TRIGGER_REACTION)
            except: pass
            return

        # それ以外はメモ追記フロー
        await self._process_memo_flow(message)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.channel_id != self.channel_id: return
        if str(payload.emoji) != BOT_PROCESS_TRIGGER_REACTION: return
        if payload.user_id == self.bot.user.id: return

        channel = self.bot.get_channel(payload.channel_id)
        try: message = await channel.fetch_message(payload.message_id)
        except: return

        if any(r.emoji in (PROCESS_START_EMOJI, PROCESS_COMPLETE_EMOJI) and r.me for r in message.reactions): return

        try: await message.remove_reaction(payload.emoji, await self.bot.fetch_user(payload.user_id))
        except: pass
        
        await self._start_book_selection_workflow(message)

    async def _process_memo_flow(self, message: discord.Message):
        # 添付ファイル判定などは既存コードと同様
        attachment = message.attachments[0] if message.attachments else None
        input_type = "text"
        if attachment:
            if attachment.content_type in SUPPORTED_AUDIO_TYPES: input_type = "audio"
            elif attachment.content_type in SUPPORTED_IMAGE_TYPES: input_type = "image"
            else: return
        elif not message.content.strip(): return

        try:
            await message.add_reaction("🤔")
            # インデックスから選択肢を作成
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, BOOK_INDEX_PATH)
                index = json.loads(res.content.decode('utf-8'))
                # ステータスがFinished以外のものを優先表示
                active_books = [b for b in index if b.get('status') != STATUS_OPTIONS['finished']]
                other_books = [b for b in index if b.get('status') == STATUS_OPTIONS['finished']]
                books = active_books + other_books
            except: books = []

            if not books:
                # インデックスがない場合はフォルダから取得（フォールバック）
                book_files, _ = await self.get_book_list()
                options = [discord.SelectOption(label=entry.name[:90], value=entry.path_display) for entry in book_files[:25]]
            else:
                options = []
                for b in books[:25]:
                    path = f"{self.dropbox_vault_path}{READING_NOTES_PATH}/{b['filename']}"
                    options.append(discord.SelectOption(label=b['title'][:90], value=path))

            view = BookSelectView(
                self, options, original_context=message, action_type="add_memo",
                attachment=attachment, text_memo=message.content, input_type=input_type
            )
            msg = await message.reply(f"どの書籍のメモですか？", view=view, mention_author=False)
            view.bot_reply_message = msg
        except Exception as e:
            logging.error(f"Memo flow error: {e}")

    async def process_posted_memo(self, interaction, original_message, book_path, input_type, attachment, text_memo):
        recognized_text = ""
        try:
            await original_message.remove_reaction("🤔", self.bot.user)
            await original_message.add_reaction(PROCESS_START_EMOJI)
            
            if input_type == "text": recognized_text = text_memo
            elif input_type == "audio":
                # 音声処理
                async with self.session.get(attachment.url) as resp:
                    file_bytes = await resp.read()
                temp_path = pathlib.Path(f"./temp_audio_{original_message.id}.{attachment.filename.split('.')[-1]}")
                temp_path.write_bytes(file_bytes)
                try:
                    with open(temp_path, "rb") as f:
                        transcription = await self.openai_client.audio.transcriptions.create(model="whisper-1", file=f)
                    recognized_text = transcription.text
                finally:
                    try: temp_path.unlink()
                    except: pass
            elif input_type == "image":
                # 画像処理
                async with self.session.get(attachment.url) as resp:
                    file_bytes = await resp.read()
                img = Image.open(io.BytesIO(file_bytes))
                vision_prompt = ["読書メモです。テキスト化してください。", img]
                response = await self.gemini_vision_model.generate_content_async(vision_prompt)
                recognized_text = response.text.strip()

            confirm_view = ConfirmMemoView(self, book_path, recognized_text, original_message, input_type)
            # 確認メッセージは残す（ユーザーが内容を確認できるように）
            confirm_msg = await original_message.reply(
                f"**📝 {input_type} メモ:**\n>>> {recognized_text}",
                view=confirm_view, mention_author=False
            )
            confirm_view.confirmation_message = confirm_msg
            await interaction.followup.send("認識完了。確認してください。", ephemeral=True)
            
        except Exception as e:
            logging.error(f"Memo processing error: {e}")
            await original_message.add_reaction(PROCESS_ERROR_EMOJI)

    async def _save_memo_to_obsidian_and_cleanup(self, interaction, book_path, final_text, input_type, original_message, confirmation_message):
        # Obsidianへの保存処理
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, book_path)
            content = res.content.decode('utf-8')
            
            now = datetime.datetime.now(JST)
            memo_line = f"- {now.strftime('%Y-%m-%d %H:%M')} ({input_type}) {final_text}"
            new_content = update_section(content, memo_line, "## Notes")
            
            await asyncio.to_thread(self.dbx.files_upload, new_content.encode('utf-8'), book_path, mode=WriteMode('overwrite'))
            
            # 完了後の表示更新：確認メッセージを「保存済み」表示に変更して残す
            await confirmation_message.edit(content=f"✅ **保存済みメモ:**\n>>> {final_text}", view=None)
            
            # ユーザーの元の投稿は削除しない
            await original_message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            await original_message.add_reaction(PROCESS_COMPLETE_EMOJI)
            
        except Exception as e:
            logging.error(f"Save memo error: {e}")
            raise

    async def get_book_list(self, check_only=False):
        try:
            path = f"{self.dropbox_vault_path}{READING_NOTES_PATH}"
            res = await asyncio.to_thread(self.dbx.files_list_folder, path)
            files = [e for e in res.entries if isinstance(e, FileMetadata) and e.name.endswith('.md')]
            files.sort(key=lambda x: x.server_modified, reverse=True)
            return files, None
        except: return [], "Error"

    async def _fetch_google_book_data(self, title):
        if not self.google_books_api_key or not self.session: return None
        q = urllib.parse.quote_plus(title)
        url = f"https://www.googleapis.com/books/v1/volumes?q={q}&key={self.google_books_api_key}&maxResults=5&langRestrict=ja"
        try:
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return [item['volumeInfo'] for item in data.get('items', [])]
        except: pass
        return None

    async def _start_book_selection_workflow(self, message):
        try:
            await message.add_reaction(PROCESS_START_EMOJI)
            title = message.embeds[0].title if message.embeds else None
            if not title:
                 title = "Untitled"
            
            books = await self._fetch_google_book_data(title) or []
            
            view = BookCreationSelectView(self, books, message.content, None, message)
            msg = await message.reply("書籍を選択してください:", view=view)
            view.confirmation_message = msg
            await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
        except Exception as e:
            logging.error(f"Selection flow error: {e}")
            await message.add_reaction(PROCESS_ERROR_EMOJI)

async def setup(bot: commands.Bot):
    await bot.add_cog(BookCog(bot))