import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import asyncio
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import datetime
import zoneinfo
import google.generativeai as genai

# 共通関数をインポート
try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.warning("StockCog: utils/obsidian_utils.pyが見つかりません。")
    def update_section(content, text, header): return f"{content}\n\n{header}\n{text}"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
INVESTMENT_PATH = "/Investment/Stocks" # Obsidian内の保存先フォルダ

# --- リアクション定数 ---
PROCESS_START_EMOJI = '⏳'
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'

# 銘柄コードの正規表現 (例: 7203, $7203, 1234.T など)
STOCK_CODE_REGEX = re.compile(r'(?:^|[\s$])([0-9]{4})(?:[\s.]|$)')

class StockStrategyModal(discord.ui.Modal, title="新規銘柄ノート作成"):
    name = discord.ui.TextInput(
        label="銘柄名",
        placeholder="例: トヨタ自動車",
        style=discord.TextStyle.short,
        required=True
    )
    code = discord.ui.TextInput(
        label="銘柄コード",
        placeholder="例: 7203",
        style=discord.TextStyle.short,
        required=True,
        min_length=4,
        max_length=10
    )
    thesis = discord.ui.TextInput(
        label="エントリーの根拠 (Thesis)",
        style=discord.TextStyle.paragraph,
        placeholder="なぜ今買うのか？材料、テクニカル、ファンダメンタルズなど",
        required=True
    )
    strategy = discord.ui.TextInput(
        label="エグジット戦略 (利確・損切りライン)",
        style=discord.TextStyle.paragraph,
        placeholder="利確目標: 2500円 (PER15倍)\n損切り: 1900円 (サポート割れ)",
        required=True
    )

    def __init__(self, cog, original_interaction: discord.Interaction):
        super().__init__(timeout=1800)
        self.cog = cog
        self.original_interaction = original_interaction

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        code_val = self.code.value.strip()
        name_val = self.name.value.strip()
        
        # ノート内容の作成
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

## 🎯 Entry Thesis (根拠)
{self.thesis.value}

## 🚪 Exit Strategy (戦略)
{self.strategy.value}

## 📓 Logs
- {now.strftime('%Y-%m-%d %H:%M')} ノート作成

## 📝 Review (振り返り)

"""
        try:
            success = await self.cog._save_file(filename, note_content)
            if success == "EXISTS":
                await interaction.followup.send(f"⚠️ 既に `{filename}` は存在します。")
            elif success:
                await interaction.followup.send(f"✅ 銘柄ノートを作成しました: `{filename}`\n目標と戦略を忘れないようにしましょう！")
            else:
                await interaction.followup.send("❌ 作成に失敗しました。")
        except Exception as e:
            logging.error(f"StockCog: ノート作成エラー: {e}")
            await interaction.followup.send(f"❌ エラーが発生しました: {e}")

class StockCog(commands.Cog):
    """株式投資の記録と振り返りを支援するCog"""

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
                self.gemini_model = genai.GenerativeModel("gemini-3-pro-preview")
                self.is_ready = True
                logging.info("StockCog initialized.")
            except Exception as e:
                logging.error(f"StockCog init failed: {e}")

    async def _save_file(self, filename, content) -> bool | str:
        """Dropboxにファイルを保存 (EXISTS, True, Falseを返す)"""
        path = f"{self.dropbox_vault_path}{INVESTMENT_PATH}/{filename}"
        try:
            # 重複チェック（簡易）
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
        """銘柄コードからファイルパスを検索する"""
        folder_path = f"{self.dropbox_vault_path}{INVESTMENT_PATH}"
        try:
            result = await asyncio.to_thread(self.dbx.files_list_folder, folder_path)
            for entry in result.entries:
                # ファイル名が "code_" で始まるものを探す
                if entry.name.startswith(f"{code}_") and entry.name.endswith(".md"):
                    return entry.path_display
            return None
        except Exception as e:
            logging.error(f"StockCog search error: {e}")
            return None

    @app_commands.command(name="stock_new", description="新規銘柄ノートを作成し、戦略を記録します。")
    async def stock_new(self, interaction: discord.Interaction):
        if interaction.channel_id != self.channel_id:
            await interaction.response.send_message(f"このコマンドは <#{self.channel_id}> でのみ使用できます。", ephemeral=True)
            return
        await interaction.response.send_modal(StockStrategyModal(self, interaction))

    @app_commands.command(name="stock_review", description="銘柄ノートをAIが分析し、振り返りを行います。")
    @app_commands.describe(code="銘柄コード")
    async def stock_review(self, interaction: discord.Interaction, code: str):
        if not self.is_ready: return
        await interaction.response.defer()

        path = await self._find_stock_note(code)
        if not path:
            await interaction.followup.send(f"❌ コード `{code}` のノートが見つかりませんでした。", ephemeral=True)
            return

        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, path)
            content = res.content.decode('utf-8')

            # AI分析
            prompt = f"""
            あなたはプロの投資コーチです。以下の投資ノート（私のエントリー根拠、戦略、日々のログ）を読み、
            今回の取引の振り返りと、今後のための教訓をアドバイスしてください。
            
            # 評価ポイント
            1. 当初の戦略（根拠・出口）は論理的だったか？
            2. ログを見る限り、戦略通りに行動できていたか？（感情的な売買はなかったか？）
            3. 次回のトレードで改善すべき具体的なアクションは何か？

            # ノート内容
            {content}
            """
            
            response = await self.gemini_model.generate_content_async(prompt)
            review_text = response.text.strip()

            # ノートに追記
            new_content = update_section(content, f"\n{review_text}", "## Review")
            await asyncio.to_thread(
                self.dbx.files_upload,
                new_content.encode('utf-8'),
                path,
                mode=WriteMode('overwrite')
            )

            embed = discord.Embed(title=f"📊 振り返り: {code}", description=review_text[:4000], color=discord.Color.gold())
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logging.error(f"StockCog review error: {e}")
            await interaction.followup.send(f"❌ エラーが発生しました: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.channel.id != self.channel_id: return
        if message.content.startswith('/'): return

        # メッセージから銘柄コードを抽出 (例: "7203 決算よい" -> "7203")
        match = STOCK_CODE_REGEX.search(message.content)
        if not match: return

        code = match.group(1)
        path = await self._find_stock_note(code)

        if path:
            try:
                await message.add_reaction(PROCESS_START_EMOJI)
                _, res = await asyncio.to_thread(self.dbx.files_download, path)
                content = res.content.decode('utf-8')
                
                now = datetime.datetime.now(JST)
                memo_line = f"- {now.strftime('%Y-%m-%d %H:%M')} {message.content}"
                
                new_content = update_section(content, memo_line, "## Logs")
                
                await asyncio.to_thread(
                    self.dbx.files_upload,
                    new_content.encode('utf-8'),
                    path,
                    mode=WriteMode('overwrite')
                )
                await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                await message.add_reaction(PROCESS_COMPLETE_EMOJI)
                
                # 書籍機能と同様に、確認用メッセージなどは省略し、シンプルにリアクション完了とする
            except Exception as e:
                logging.error(f"StockCog memo add error: {e}")
                await message.add_reaction(PROCESS_ERROR_EMOJI)
        else:
            # ノートがない場合はリアクションで通知（オプション：ここで新規作成を促すことも可能）
            await message.add_reaction('❓') # 「ノートが見つからない」の意味

async def setup(bot: commands.Bot):
    await bot.add_cog(StockCog(bot))