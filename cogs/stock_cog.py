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
import google.generativeai as genai

# 共通関数をインポート
try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.warning("StockCog: utils/obsidian_utils.pyが見つかりません。")
    def update_section(content, text, header): return f"{content}\n{header}\n{text}"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
INVESTMENT_PATH = "/Investment/Stocks" 

# --- リアクション定数 ---
PROCESS_START_EMOJI = '⏳'
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'
SELECT_EMOJI = '🤔'

STOCK_CODE_REGEX = re.compile(r'(?:^|[\s$])([0-9]{4}|[a-zA-Z]{1,5})(?:[\s.]|$)')

# --- UI Components ---

class StockSelectView(discord.ui.View):
    def __init__(self, cog, stock_files, memo_content, original_message):
        super().__init__(timeout=60)
        self.cog = cog
        self.memo_content = memo_content
        self.original_message = original_message
        
        options = []
        for file in stock_files[:25]:
            label = os.path.splitext(file.name)[0][:100]
            options.append(discord.SelectOption(
                label=label,
                value=file.path_display
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
        selected_path = interaction.data["values"][0]
        
        try:
            await self.original_message.remove_reaction(SELECT_EMOJI, self.cog.bot.user)
            await self.original_message.add_reaction(PROCESS_START_EMOJI)
        except: pass

        success = await self.cog._append_memo_to_note(selected_path, self.memo_content)
        
        if success:
            await interaction.followup.send(f"✅ `{os.path.basename(selected_path)}` にメモを追加しました。", ephemeral=True)
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
        try:
            await interaction.message.edit(view=None, content="✅ 選択完了")
        except: pass


class StockStrategyModal(discord.ui.Modal, title="新規銘柄ノート"):
    name = discord.ui.TextInput(
        label="銘柄名",
        placeholder="例: トヨタ自動車, Apple",
        style=discord.TextStyle.short,
        required=True
    )
    code = discord.ui.TextInput(
        label="銘柄コード / ティッカー",
        placeholder="例: 7203, AAPL",
        style=discord.TextStyle.short,
        required=True,
        min_length=1, 
        max_length=10
    )
    thesis = discord.ui.TextInput(
        label="エントリーの根拠 (Thesis)",
        style=discord.TextStyle.paragraph,
        placeholder="なぜ今買うのか？ カタリスト、テクニカル、ファンダメンタルズなど",
        required=True
    )
    strategy = discord.ui.TextInput(
        label="エグジット戦略",
        style=discord.TextStyle.paragraph,
        placeholder="目標株価、損切りライン",
        required=True
    )

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
        # Obsidianの見出しは英語
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
            if success == "EXISTS":
                await interaction.followup.send(f"⚠️ `{filename}` は既に存在します。", ephemeral=True)
            elif success:
                await interaction.followup.send(f"✅ 銘柄ノートを作成しました: `{filename}`", ephemeral=True)
            else:
                await interaction.followup.send("❌ 作成に失敗しました。", ephemeral=True)
        except Exception as e:
            logging.error(f"StockCog: Create note error: {e}")
            await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)

class StockCog(commands.Cog):
    """株式投資の記録用Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = int(os.getenv("STOCK_LOG_CHANNEL_ID", 0))
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        self.dbx = None
        self.is_ready = False

        if all([self.channel_id, self.dropbox_refresh_token, self.gemini_api_key]):
            try:
                self.dbx = dropbox.Dropbox(
                    oauth2_refresh_token=self.dropbox_refresh_token,
                    app_key=self.dropbox_app_key,
                    app_secret=self.dropbox_app_secret
                )
                genai.configure(api_key=self.gemini_api_key)
                self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
                self.is_ready = True
                logging.info("StockCog initialized.")
            except Exception as e:
                logging.error(f"StockCog init failed: {e}")

    async def _save_file(self, filename, content) -> bool | str:
        path = f"{self.dropbox_vault_path}{INVESTMENT_PATH}/{filename}"
        try:
            try:
                self.dbx.files_get_metadata(path)
                return "EXISTS"
            except: pass

            await asyncio.to_thread(
                self.dbx.files_upload,
                content.encode('utf-8'),
                path,
                mode=WriteMode('add')
            )
            return True
        except Exception as e:
            logging.error(f"StockCog save error: {e}")
            return False

    async def _find_stock_note(self, code: str) -> str | None:
        folder_path = f"{self.dropbox_vault_path}{INVESTMENT_PATH}"
        try:
            result = await asyncio.to_thread(self.dbx.files_list_folder, folder_path)
            for entry in result.entries:
                if entry.name.startswith(f"{code}_") and entry.name.endswith(".md"):
                    return entry.path_display
            return None
        except Exception as e:
            logging.error(f"StockCog search error: {e}")
            return None

    async def _get_stock_list(self):
        try:
            folder_path = f"{self.dropbox_vault_path}{INVESTMENT_PATH}"
            result = await asyncio.to_thread(self.dbx.files_list_folder, folder_path)
            files = [
                e for e in result.entries 
                if isinstance(e, FileMetadata) and e.name.endswith('.md')
            ]
            files.sort(key=lambda x: x.server_modified, reverse=True)
            return files
        except Exception as e:
            logging.error(f"StockCog list error: {e}")
            return []

    async def _append_memo_to_note(self, path: str, content_text: str) -> bool:
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, path)
            current_content = res.content.decode('utf-8')
            
            now = datetime.datetime.now(JST)
            memo_line = f"- {now.strftime('%Y-%m-%d %H:%M')} {content_text}"
            
            # Obsidianの見出しは英語 (## Logs)
            new_content = update_section(current_content, memo_line, "## Logs")
            
            await asyncio.to_thread(
                self.dbx.files_upload,
                new_content.encode('utf-8'),
                path,
                mode=WriteMode('overwrite')
            )
            return True
        except Exception as e:
            logging.error(f"StockCog append error: {e}")
            return False

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

        path = await self._find_stock_note(code.upper())
        if not path:
            await interaction.followup.send(f"❌ コード `{code}` のノートが見つかりませんでした。", ephemeral=True)
            return

        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, path)
            content = res.content.decode('utf-8')

            prompt = f"""
            あなたはプロの投資コーチです。以下の投資ノート（エントリーの根拠、戦略、ログ）を読み、
            今回のトレードの振り返りと今後のための教訓を提示してください。
            
            # 評価ポイント
            1. 当初の戦略（根拠・エグジット）は論理的だったか？
            2. ログに基づき、戦略通りに行動できたか？（感情的なトレードはなかったか？）
            3. 次回のトレードに向けて改善すべき具体的なアクションは何か？

            # ノートの内容
            {content}
            """
            
            response = await self.gemini_model.generate_content_async(prompt)
            review_text = response.text.strip()

            # Obsidianの見出しは英語 (## Review)
            new_content = update_section(content, f"\n{review_text}", "## Review")
            await asyncio.to_thread(
                self.dbx.files_upload,
                new_content.encode('utf-8'),
                path,
                mode=WriteMode('overwrite')
            )

            # Discordの表示は日本語
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
            path = await self._find_stock_note(code)

            if path:
                try:
                    await message.add_reaction(PROCESS_START_EMOJI)
                    success = await self._append_memo_to_note(path, message.content)
                    
                    await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                    if success:
                        await message.add_reaction(PROCESS_COMPLETE_EMOJI)
                    else:
                        await message.add_reaction(PROCESS_ERROR_EMOJI)
                except Exception as e:
                    logging.error(f"StockCog auto-append error: {e}")
                    await message.add_reaction(PROCESS_ERROR_EMOJI)
            else:
                await message.add_reaction('❓')
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