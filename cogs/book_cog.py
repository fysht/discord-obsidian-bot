import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import asyncio
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

# Google Drive API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.error("BookCog: utils/obsidian_utils.pyが見つかりません。")
    def update_section(content, text, header): return f"{content}\n\n{header}\n{text}"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
READING_NOTES_FOLDER = "Reading Notes"
BOOK_INDEX_FILE = "book_index.json"
BOT_FOLDER = ".bot"
DAILY_NOTE_SECTION = "## Reading Notes"

SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

# --- UI Components (変更なしのため省略せず記述) ---
# ... (BookDetailView, BookListView, BookMemoEditModal, BookManualInputModal, ConfirmMemoView, BookStatusView, BookCreationSelectView, BookSelectView は元のコードと同じですが、呼び出し元のCogメソッドが変わるため、クラス定義はそのまま維持し、Cog内のメソッド呼び出しだけ整合性を保ちます)
# ※ 長くなるため、UIコンポーネントのクラス定義は元のコードと同じものを使用してください。
# ここではCog本体の修正に集中します。

# (UIコンポーネントの再掲は省略し、Cogクラスのみ提示します)
# 実際のファイル作成時は、元のUIコンポーネントコードをここに含めてください。

# --- UI Components ---
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
        
        start = self.page * self.items_per_page
        end = start + self.items_per_page
        current_items = self.books[start:end]
        
        options = []
        for i, book in enumerate(current_items):
            label = book.get('title', '無題')[:90]
            authors = ", ".join(book.get('authors', []))[:90]
            status_emoji = "📖"
            if book.get('status') == "Finished": status_emoji = "✅"
            elif book.get('status') == "Wishlist": status_emoji = "✨"
            
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

class BookMemoEditModal(discord.ui.Modal, title="読書メモの編集"):
    memo_text = discord.ui.TextInput(label="認識されたテキスト", style=discord.TextStyle.paragraph, required=True, max_length=1500)
    def __init__(self, cog, book_path, initial_text, original_message, confirmation_message, input_type):
        super().__init__(timeout=1800); self.cog=cog; self.book_path=book_path; self.memo_text.default=initial_text; self.original_message=original_message; self.confirmation_message=confirmation_message; self.input_type=input_type
    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try: await self.cog._save_memo_to_obsidian_and_cleanup(interaction, self.book_path, self.memo_text.value, self.input_type, self.original_message, self.confirmation_message); await interaction.followup.send("✅ 保存しました。", ephemeral=True, delete_after=10)
        except Exception as e: await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)

class BookManualInputModal(discord.ui.Modal, title="書籍情報の手動入力"):
    title_input = discord.ui.TextInput(label="書籍のタイトル", style=discord.TextStyle.short, required=True, max_length=200)
    authors_input = discord.ui.TextInput(label="著者名（カンマ区切り）", style=discord.TextStyle.short, required=False)
    published_input = discord.ui.TextInput(label="出版日", style=discord.TextStyle.short, required=False, placeholder="2024-01-01")
    cover_url_input = discord.ui.TextInput(label="カバー画像URL", style=discord.TextStyle.short, required=False)
    description_input = discord.ui.TextInput(label="概要", style=discord.TextStyle.paragraph, required=False, max_length=1000)

    def __init__(self, cog, source_url, original_message, confirmation_message):
        super().__init__(timeout=1800); self.cog=cog; self.source_url=source_url; self.original_message=original_message; self.confirmation_message=confirmation_message

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        book_data = {"title": self.title_input.value, "authors": [a.strip() for a in self.authors_input.value.split(',')] if self.authors_input.value else ["著者不明"], "publishedDate": self.published_input.value or "N/A", "description": self.description_input.value or "N/A", "imageLinks": {"thumbnail": self.cover_url_input.value} if self.cover_url_input.value else {}}
        try:
            res = await self.cog._save_note_to_obsidian(book_data, self.source_url)
            if res == True: 
                try: await self.confirmation_message.delete()
                except: pass
                await interaction.followup.send(f"✅ ノート「{book_data['title']}」を作成しました。", ephemeral=True, delete_after=10)
            elif res == "EXISTS": await interaction.followup.send(f"⚠️ そのノートは既に存在します。", ephemeral=True)
        except Exception as e: await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)

class ConfirmMemoView(discord.ui.View):
    def __init__(self, cog, book_path, recognized_text, original_message, input_type):
        super().__init__(timeout=1800); self.cog=cog; self.book_path=book_path; self.recognized_text=recognized_text; self.original_message=original_message; self.input_type=input_type; self.confirmation_message=None
    @discord.ui.button(label="✅ このまま保存", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try: await self.cog._save_memo_to_obsidian_and_cleanup(interaction, self.book_path, self.recognized_text, self.input_type, self.original_message, self.confirmation_message); await interaction.followup.send("✅ 保存しました。", ephemeral=True, delete_after=10)
        except Exception as e: await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)
        finally: self.stop()
    @discord.ui.button(label="✏️ 編集する", style=discord.ButtonStyle.primary)
    async def edit(self, interaction, button): await interaction.response.send_modal(BookMemoEditModal(self.cog, self.book_path, self.recognized_text, self.original_message, self.confirmation_message, self.input_type)); self.stop()

class BookStatusView(discord.ui.View):
    def __init__(self, cog, book_path, original_context, current_status):
        super().__init__(timeout=300); self.cog=cog; self.book_path=book_path; self.original_context=original_context; self.current_status=current_status; self.add_status_buttons()
    def add_status_buttons(self):
        options = {"wishlist": "Wishlist", "to_read": "To Read", "reading": "Reading", "finished": "Finished"}
        for key, label in options.items():
            is_current = (label.lower() == self.current_status.lower())
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary if is_current else discord.ButtonStyle.primary, custom_id=f"status_{key}", disabled=is_current)
            btn.callback = self.handle_status_change; self.add_item(btn)
    async def handle_status_change(self, interaction):
        await interaction.response.defer(ephemeral=True)
        options = {"wishlist": "Wishlist", "to_read": "To Read", "reading": "Reading", "finished": "Finished"}
        new_status = options.get(interaction.data["custom_id"].replace("status_", ""))
        if await self.cog._update_book_status(self.book_path, new_status):
            await interaction.followup.send(f"✅ ステータス変更: **{new_status}**", ephemeral=True, delete_after=10)
            try: 
                if isinstance(self.original_context, discord.Interaction): await self.original_context.delete_original_response()
                elif isinstance(self.original_context, discord.Message): await self.original_context.delete()
            except: pass
        else: await interaction.followup.send(f"❌ 失敗しました。", ephemeral=True)
        self.stop()

class BookCreationSelectView(discord.ui.View):
    def __init__(self, cog, book_results, source_url, embed_image_url_fallback, original_message):
        super().__init__(timeout=600); self.cog=cog; self.book_results=book_results; self.source_url=source_url; self.embed_image_url_fallback=embed_image_url_fallback; self.original_message=original_message; self.confirmation_message=None
        options = [discord.SelectOption(label=b.get("title","")[:95], description=", ".join(b.get("authors",[]))[:95], value=str(i)) for i, b in enumerate(book_results[:25])]
        if not options: options.append(discord.SelectOption(label="候補なし", value="-1"))
        select = discord.ui.Select(placeholder="書籍を選択...", options=options); select.callback = self.select_callback; self.add_item(select)
        manual = discord.ui.Button(label="手動入力", style=discord.ButtonStyle.primary, row=1); manual.callback = self.manual_input_callback; self.add_item(manual)
    async def select_callback(self, interaction):
        await interaction.response.defer(ephemeral=True); idx = int(interaction.data["values"][0])
        if idx == -1: return
        res = await self.cog._save_note_to_obsidian(self.book_results[idx], self.source_url, self.embed_image_url_fallback)
        if res == True: 
            try: await self.confirmation_message.delete()
            except: pass
            await interaction.followup.send(f"✅ 作成しました。", ephemeral=True, delete_after=10)
        elif res == "EXISTS": await interaction.followup.send(f"⚠️ 既に存在します。", ephemeral=True)
        else: await interaction.followup.send("❌ 失敗しました。", ephemeral=True)
        self.stop()
    async def manual_input_callback(self, interaction): await interaction.response.send_modal(BookManualInputModal(self.cog, self.source_url, self.original_message, self.confirmation_message)); self.stop()

class BookSelectView(discord.ui.View):
    def __init__(self, cog, book_options, original_context, action_type, attachment=None, text_memo=None, input_type=None):
        super().__init__(timeout=600); self.cog=cog; self.original_context=original_context; self.action_type=action_type; self.attachment=attachment; self.text_memo=text_memo; self.input_type=input_type; self.bot_reply_message=None
        select = discord.ui.Select(placeholder="書籍を選択...", options=book_options); select.callback = self.select_callback; self.add_item(select)
    async def select_callback(self, interaction):
        path = interaction.data["values"][0]
        if self.action_type == "status":
            await interaction.response.defer(ephemeral=True); stat = await self.cog._get_current_status(path)
            view = BookStatusView(self.cog, path, self.original_context, stat)
            await self.original_context.edit_original_response(content=f"ステータス変更: **{os.path.basename(path)}**", view=view)
        elif self.action_type == "add_memo":
            try: 
                if self.bot_reply_message: await self.bot_reply_message.delete()
            except: pass
            await self.cog.process_posted_memo(interaction, self.original_context, path, self.input_type, self.attachment, self.text_memo)
        self.stop()

# --- BookCog ---
class BookCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = int(os.getenv("BOOK_NOTE_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.google_books_api_key = os.getenv("GOOGLE_BOOKS_API_KEY")
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        self.session = aiohttp.ClientSession()
        self.openai_client = openai.AsyncOpenAI(api_key=self.openai_api_key)
        genai.configure(api_key=self.gemini_api_key)
        self.gemini_vision_model = genai.GenerativeModel("gemini-2.5-pro")
        self.is_ready = bool(self.drive_folder_id)

    def _get_drive_service(self):
        creds = None
        if os.path.exists(TOKEN_FILE):
            try: creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            except: pass
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try: creds.refresh(Request()); open(TOKEN_FILE,'w').write(creds.to_json())
                except: return None
            else: return None
        return build('drive', 'v3', credentials=creds)

    def _find_file(self, service, parent_id, name):
        res = service.files().list(q=f"'{parent_id}' in parents and name = '{name}' and trashed = false", fields="files(id)").execute()
        files = res.get('files', [])
        return files[0]['id'] if files else None

    def _create_folder(self, service, parent_id, name):
        f = service.files().create(body={'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}, fields='id').execute()
        return f.get('id')

    def _read_json(self, service, file_id):
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=file_id))
        done=False
        while not done: _, done = downloader.next_chunk()
        return json.loads(fh.getvalue().decode('utf-8'))

    def _write_json(self, service, parent_id, name, data, file_id=None):
        media = MediaIoBaseUpload(io.BytesIO(json.dumps(data, ensure_ascii=False).encode('utf-8')), mimetype='application/json')
        if file_id: service.files().update(fileId=file_id, media_body=media).execute()
        else: service.files().create(body={'name': name, 'parents': [parent_id]}, media_body=media).execute()

    def _read_text(self, service, file_id):
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=file_id))
        done=False
        while not done: _, done = downloader.next_chunk()
        return fh.getvalue().decode('utf-8')

    def _update_text(self, service, file_id, content):
        service.files().update(fileId=file_id, media_body=MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')).execute()
    
    def _create_text(self, service, parent_id, name, content):
        service.files().create(body={'name': name, 'parents': [parent_id], 'mimeType': 'text/markdown'}, media_body=MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')).execute()

    async def get_book_list(self):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return [], "Error"
        
        r_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, READING_NOTES_FOLDER)
        if not r_folder: return [], None
        
        # 簡易リスト取得
        res = await loop.run_in_executor(None, lambda: service.files().list(q=f"'{r_folder}' in parents and mimeType = 'text/markdown' and trashed = false", fields="files(id, name)").execute())
        files = []
        for f in res.get('files', []):
            files.append(type('obj', (object,), {'name': f['name'], 'path_display': f['id']})) # path_displayにIDを入れるハック
        return files, None

    async def _save_note_to_obsidian(self, book_data, source_url, embed_image_url_fallback=None):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return False

        title = book_data.get("title", "Untitled")
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)
        filename = f"{safe_title}.md"
        
        r_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, READING_NOTES_FOLDER)
        if not r_folder: r_folder = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, READING_NOTES_FOLDER)

        existing = await loop.run_in_executor(None, self._find_file, service, r_folder, filename)
        if existing: return "EXISTS"

        thumbnail = book_data.get("imageLinks", {}).get("thumbnail", embed_image_url_fallback or "")
        now = datetime.datetime.now(JST)
        content = f"""---
title: "{title}"
authors: {json.dumps(book_data.get('authors', []), ensure_ascii=False)}
published: {book_data.get('publishedDate', 'N/A')}
source: {source_url}
tags: [book]
status: "To Read"
created: {now.isoformat()}
cover: "{thumbnail}"
---
## Summary
{book_data.get('description', 'N/A')}

## Notes

## Actions
"""
        await loop.run_in_executor(None, self._create_text, service, r_folder, filename, content)
        
        # Index Update
        b_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, BOT_FOLDER)
        if not b_folder: b_folder = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, BOT_FOLDER)
        
        idx_file = await loop.run_in_executor(None, self._find_file, service, b_folder, BOOK_INDEX_FILE)
        index = []
        if idx_file: index = await loop.run_in_executor(None, self._read_json, service, idx_file)
        
        index.insert(0, {
            "title": title, "authors": book_data.get('authors', []), "filename": filename,
            "status": "To Read", "cover": thumbnail, "source": book_data.get('infoLink', ''),
            "added_at": now.isoformat()
        })
        await loop.run_in_executor(None, self._write_json, service, b_folder, BOOK_INDEX_FILE, index, idx_file)
        
        # Daily Note Update
        daily_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, "DailyNotes")
        if not daily_folder: daily_folder = await loop.run_in_executor(None, self._create_folder, service, self.drive_folder_id, "DailyNotes")
        
        d_filename = f"{now.strftime('%Y-%m-%d')}.md"
        d_file = await loop.run_in_executor(None, self._find_file, service, daily_folder, d_filename)
        cur = ""
        if d_file: cur = await loop.run_in_executor(None, self._read_text, service, d_file)
        else: cur = f"# Daily Note {now.strftime('%Y-%m-%d')}\n"
        
        new = update_section(cur, f"- [ ] 📚 Start reading: [[{safe_title}]]", DAILY_NOTE_SECTION)
        if d_file: await loop.run_in_executor(None, self._update_text, service, d_file, new)
        else: await loop.run_in_executor(None, self._create_text, service, daily_folder, d_filename, new)
        
        return True

    async def _update_book_status(self, book_path, new_status):
        # book_pathはここではDrive File IDまたはNameが来る想定だが、BookSelectViewではIDを渡すように変更が必要
        # 簡易化のため、IDが渡ってくると仮定
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return False

        # IDか名前か判別が難しいが、IDと仮定して処理
        try:
            content = await loop.run_in_executor(None, self._read_text, service, book_path)
            new_content = re.sub(r'status: ".*?"', f'status: "{new_status}"', content, count=1)
            await loop.run_in_executor(None, self._update_text, service, book_path, new_content)
            
            # Index更新 (ファイル名取得が必要)
            # 実際にはメタデータ取得してファイル名特定が必要
            meta = await loop.run_in_executor(None, lambda: service.files().get(fileId=book_path, fields='name').execute())
            filename = meta['name']
            
            b_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, BOT_FOLDER)
            idx_file = await loop.run_in_executor(None, self._find_file, service, b_folder, BOOK_INDEX_FILE)
            if idx_file:
                index = await loop.run_in_executor(None, self._read_json, service, idx_file)
                for b in index:
                    if b['filename'] == filename: b['status'] = new_status; break
                await loop.run_in_executor(None, self._write_json, service, b_folder, BOOK_INDEX_FILE, index, idx_file)
            return True
        except: return False

    async def _get_current_status(self, book_path):
        # IDから読み込んでステータス抽出
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        try:
            content = await loop.run_in_executor(None, self._read_text, service, book_path)
            m = re.search(r'status: "(.*?)"', content)
            return m.group(1) if m else "To Read"
        except: return "To Read"

    # --- Handlers (on_message, etc.) はロジック変更なし、process_posted_memo内で _save_memo... を呼ぶ ---
    # _save_memo_to_obsidian_and_cleanup もDrive API版に書き換え
    async def _save_memo_to_obsidian_and_cleanup(self, interaction, book_path, final_text, input_type, original_message, confirmation_message):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        
        content = await loop.run_in_executor(None, self._read_text, service, book_path)
        now = datetime.datetime.now(JST)
        line = f"- {now.strftime('%Y-%m-%d %H:%M')} ({input_type}) {final_text}"
        new = update_section(content, line, "## Notes")
        await loop.run_in_executor(None, self._update_text, service, book_path, new)
        
        await confirmation_message.edit(content=f"✅ **保存済みメモ:**\n>>> {final_text}", view=None)
        await original_message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
        await original_message.add_reaction(PROCESS_COMPLETE_EMOJI)

    # _fetch_google_book_data は変更なし
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
    
    # イベントリスナーやコマンドハンドラは、メソッド呼び出しを維持すればOK
    # BookSelectViewで path ではなく ID を渡すように変更が必要な点に注意 (get_book_listで対応済み)

async def setup(bot): await bot.add_cog(BookCog(bot))