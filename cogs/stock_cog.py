import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import asyncio
import datetime
import zoneinfo
# --- 新しいライブラリ ---
from google import genai
# ----------------------

# Google Drive API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
import io

try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.warning("StockCog: utils/obsidian_utils.pyが見つかりません。")
    def update_section(content, text, header): return f"{content}\n{header}\n{text}"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
INVESTMENT_FOLDER = "Investment"
STOCKS_FOLDER = "Stocks"

PROCESS_START_EMOJI = '⏳'
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'
SELECT_EMOJI = '🤔'
STOCK_CODE_REGEX = re.compile(r'(?:^|[\s$])([0-9]{4}|[a-zA-Z]{1,5})(?:[\s.]|$)')

SCOPES = ['https://www.googleapis.com/auth/drive']
TOKEN_FILE = 'token.json'

class StockSelectView(discord.ui.View):
    def __init__(self, cog, stock_files, memo_content, original_message):
        super().__init__(timeout=60)
        self.cog = cog
        self.memo_content = memo_content
        self.original_message = original_message
        
        options = []
        for file in stock_files[:25]:
            label = os.path.splitext(file['name'])[0][:100]
            options.append(discord.SelectOption(
                label=label,
                value=file['id']
            ))
            
        select = discord.ui.Select(
            placeholder="メモを追加する銘柄を選択...",
            options=options,
            min_values=1,
            max_values=1
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        selected_id = interaction.data["values"][0]
        
        try:
            await self.original_message.remove_reaction(SELECT_EMOJI, self.cog.bot.user)
            await self.original_message.add_reaction(PROCESS_START_EMOJI)
        except: pass

        success = await self.cog._append_memo_to_note(selected_id, self.memo_content)
        
        if success:
            await interaction.followup.send(f"✅ メモを追加しました。", ephemeral=True)
            try:
                await self.original_message.remove_reaction(PROCESS_START_EMOJI, self.cog.bot.user)
                await self.original_message.add_reaction(PROCESS_COMPLETE_EMOJI)
            except: pass
        else:
            await interaction.followup.send("❌ メモの追加に失敗しました。", ephemeral=True)
            try:
                await self.original_message.remove_reaction(PROCESS_START_EMOJI, self.cog.bot.user)
                await self.original_message.add_reaction(PROCESS_ERROR_EMOJI)
            except: pass
        
        self.stop()
        try: await interaction.message.edit(view=None, content="✅ 選択完了")
        except: pass

class StockStrategyModal(discord.ui.Modal, title="新規銘柄ノート"):
    name = discord.ui.TextInput(label="銘柄名", placeholder="例: トヨタ自動車, Apple", style=discord.TextStyle.short, required=True)
    code = discord.ui.TextInput(label="銘柄コード", placeholder="例: 7203, AAPL", style=discord.TextStyle.short, required=True, min_length=1, max_length=10)
    thesis = discord.ui.TextInput(label="エントリーの根拠 (Thesis)", style=discord.TextStyle.paragraph, placeholder="なぜ今買うのか？", required=True)
    strategy = discord.ui.TextInput(label="エグジット戦略", style=discord.TextStyle.paragraph, placeholder="目標株価、損切りライン", required=True)

    def __init__(self, cog, original_interaction: discord.Interaction):
        super().__init__(timeout=1800)
        self.cog = cog
        self.original_interaction = original_interaction

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        code_val = self.code.value.strip().upper()
        name_val = self.name.value.strip()
        now = datetime.datetime.now(JST)
        filename = f"{code_val}_{name_val}.md"
        note_content = f"""---
code: "{code_val}"
name: "{name_val}"
status: "Watching"
created: {now.isoformat()}
tags: [stock, investment]
---
# {name_val} ({code_val})
## Entry Thesis
{self.thesis.value}
## Exit Strategy
{self.strategy.value}
## Logs
- {now.strftime('%Y-%m-%d %H:%M')} Created note
## Review
"""
        try:
            success = await self.cog._save_file(filename, note_content)
            if success == "EXISTS": await interaction.followup.send(f"⚠️ `{filename}` は既に存在します。", ephemeral=True)
            elif success: await interaction.followup.send(f"✅ 銘柄ノートを作成しました: `{filename}`", ephemeral=True)
            else: await interaction.followup.send("❌ 作成に失敗しました。", ephemeral=True)
        except Exception as e:
            logging.error(f"StockCog: Create note error: {e}")
            await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)

class StockCog(commands.Cog):
    """株式投資の記録用Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = int(os.getenv("STOCK_LOG_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        # --- Client初期化 ---
        if self.gemini_api_key:
            self.gemini_client = genai.Client(api_key=self.gemini_api_key)
        else:
            self.gemini_client = None
        # ------------------
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
        res = service.files().list(q=f"'{parent_id}' in parents and name = '{name}' and trashed = false", fields="files(id, name)").execute()
        files = res.get('files', [])
        return files[0] if files else None

    async def _get_stocks_folder(self, service):
        loop = asyncio.get_running_loop()
        inv_folder = await loop.run_in_executor(None, self._find_file, service, self.drive_folder_id, INVESTMENT_FOLDER)
        if not inv_folder: inv_folder = await loop.run_in_executor(None, lambda: service.files().create(body={'name': INVESTMENT_FOLDER, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [self.drive_folder_id]}, fields='id').execute())
        else: inv_folder = inv_folder['id'] 
        
        if isinstance(inv_folder, dict): inv_folder = inv_folder['id']

        stocks_folder = await loop.run_in_executor(None, self._find_file, service, inv_folder, STOCKS_FOLDER)
        if not stocks_folder: stocks_folder = await loop.run_in_executor(None, lambda: service.files().create(body={'name': STOCKS_FOLDER, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [inv_folder]}, fields='id').execute())
        
        if isinstance(stocks_folder, dict): stocks_folder = stocks_folder['id']
        else: stocks_folder = stocks_folder.get('id')
        
        return stocks_folder

    async def _save_file(self, filename, content) -> bool | str:
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return False

        stocks_folder_id = await self._get_stocks_folder(service)
        existing = await loop.run_in_executor(None, self._find_file, service, stocks_folder_id, filename)
        if existing: return "EXISTS"

        media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')
        await loop.run_in_executor(None, lambda: service.files().create(body={'name': filename, 'parents': [stocks_folder_id], 'mimeType': 'text/markdown'}, media_body=media).execute())
        return True

    async def _find_stock_note_id(self, code: str) -> str | None:
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        stocks_folder_id = await self._get_stocks_folder(service)
        
        q = f"'{stocks_folder_id}' in parents and name contains '{code}_' and mimeType = 'text/markdown' and trashed = false"
        res = await loop.run_in_executor(None, lambda: service.files().list(q=q, fields="files(id, name)").execute())
        files = res.get('files', [])
        return files[0]['id'] if files else None

    async def _get_stock_list(self):
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        stocks_folder_id = await self._get_stocks_folder(service)
        
        res = await loop.run_in_executor(None, lambda: service.files().list(q=f"'{stocks_folder_id}' in parents and mimeType = 'text/markdown' and trashed = false", fields="files(id, name, modifiedTime)").execute())
        files = res.get('files', [])
        files.sort(key=lambda x: x.get('modifiedTime', ''), reverse=True)
        return files

    async def _append_memo_to_note(self, file_id: str, content_text: str) -> bool:
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=file_id))
        done=False
        while not done: _, done = downloader.next_chunk()
        current_content = fh.getvalue().decode('utf-8')
        
        now = datetime.datetime.now(JST)
        memo_line = f"- {now.strftime('%Y-%m-%d %H:%M')} {content_text}"
        new_content = update_section(current_content, memo_line, "## Logs")
        
        media = MediaIoBaseUpload(io.BytesIO(new_content.encode('utf-8')), mimetype='text/markdown')
        await loop.run_in_executor(None, lambda: service.files().update(fileId=file_id, media_body=media).execute())
        return True

    @app_commands.command(name="stock_new", description="新規の銘柄ノートを作成します。")
    async def stock_new(self, interaction: discord.Interaction):
        if interaction.channel_id != self.channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.channel_id}> でのみ使用できます。", ephemeral=True)
            return
        await interaction.response.send_modal(StockStrategyModal(self, interaction))

    @app_commands.command(name="stock_review", description="AIが銘柄ノートを分析し、振り返りを行います。")
    @app_commands.describe(code="銘柄コード")
    async def stock_review(self, interaction: discord.Interaction, code: str):
        if not self.is_ready: return
        await interaction.response.defer()

        file_id = await self._find_stock_note_id(code.upper())
        if not file_id:
            await interaction.followup.send(f"❌ コード `{code}` のノートが見つかりませんでした。", ephemeral=True)
            return

        try:
            loop = asyncio.get_running_loop()
            service = await loop.run_in_executor(None, self._get_drive_service)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=file_id))
            done=False
            while not done: _, done = downloader.next_chunk()
            content = fh.getvalue().decode('utf-8')

            prompt = f"""
            あなたはプロの投資コーチです。以下の投資ノート（エントリーの根拠、戦略、ログ）を読み、
            今回のトレードの振り返りと今後のための教訓を提示してください。
            # ノートの内容
            {content}
            """
            
            # --- 生成メソッド変更 ---
            if self.gemini_client:
                response = await self.gemini_client.aio.models.generate_content(
                    model='gemini-2.5-pro',
                    contents=prompt
                )
                review_text = response.text.strip()
            else:
                review_text = "API Key error."
            # ----------------------

            new_content = update_section(content, f"\n{review_text}", "## Review")
            
            media = MediaIoBaseUpload(io.BytesIO(new_content.encode('utf-8')), mimetype='text/markdown')
            await loop.run_in_executor(None, lambda: service.files().update(fileId=file_id, media_body=media).execute())

            embed = discord.Embed(title=f"📊 振り返り: {code}", description=review_text[:4000], color=discord.Color.gold())
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logging.error(f"StockCog review error: {e}")
            await interaction.followup.send(f"❌ エラー: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.channel.id != self.channel_id: return
        if message.content.startswith('/'): return

        match = STOCK_CODE_REGEX.search(message.content)
        if match:
            code = match.group(1).upper()
            file_id = await self._find_stock_note_id(code)

            if file_id:
                try:
                    await message.add_reaction(PROCESS_START_EMOJI)
                    success = await self._append_memo_to_note(file_id, message.content)
                    await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                    if success: await message.add_reaction(PROCESS_COMPLETE_EMOJI)
                    else: await message.add_reaction(PROCESS_ERROR_EMOJI)
                except Exception as e:
                    logging.error(f"StockCog auto-append error: {e}")
                    await message.add_reaction(PROCESS_ERROR_EMOJI)
            else: await message.add_reaction('❓')
            return

        if message.content.strip() or message.attachments:
            try:
                await message.add_reaction(SELECT_EMOJI)
                stock_files = await self._get_stock_list()
                if not stock_files:
                    await message.reply("⚠️ 銘柄ノートが見つかりません。", delete_after=10)
                    await message.remove_reaction(SELECT_EMOJI, self.bot.user)
                    return
                view = StockSelectView(self, stock_files, message.content, message)
                await message.reply("📝 このメモを追加する銘柄を選択してください:", view=view)
            except Exception as e:
                logging.error(f"StockCog select flow error: {e}")
                await message.remove_reaction(SELECT_EMOJI, self.bot.user)
                await message.add_reaction(PROCESS_ERROR_EMOJI)

async def setup(bot: commands.Bot):
    await bot.add_cog(StockCog(bot))