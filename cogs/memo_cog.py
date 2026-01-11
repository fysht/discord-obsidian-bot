import os
import discord
from discord.ext import commands
import logging
import aiohttp
import dropbox
from dropbox.files import WriteMode
from dropbox.exceptions import ApiError
from datetime import datetime
import zoneinfo
import asyncio

# 共通関数をインポート
try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.warning("MemoCog: utils/obsidian_utils.pyが見つかりません。")
    def update_section(content, text, header): return f"{content}\n\n{header}\n{text}"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
MEMO_HEADER = "## Memo"

class MemoCog(commands.Cog):
    """
    Discordのメモチャンネルへの投稿を、Obsidianのデイリーノートの「## Memo」セクションに転記するCog。
    デジタルでのメモ書き用。手書き画像はHandwrittenMemoCogが担当する。
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        
        # Dropbox設定
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        
        self.dbx = None
        if all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            try:
                self.dbx = dropbox.Dropbox(
                    oauth2_refresh_token=self.dropbox_refresh_token,
                    app_key=self.dropbox_app_key,
                    app_secret=self.dropbox_app_secret
                )
            except Exception as e:
                logging.error(f"MemoCog: Dropbox Init Error: {e}")
        else:
            logging.warning("MemoCog: Dropbox credentials missing.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """メモチャンネルへの投稿を監視して保存"""
        # Bot自身の投稿や、対象外のチャンネルは無視
        if message.author.bot:
            return
        if message.channel.id != self.memo_channel_id:
            return
        
        # コンテンツがない（画像のみ等）場合は無視
        # ※画像のみの場合はHandwrittenMemoCogが処理する想定
        content = message.content.strip()
        if not content:
            return

        # コマンドっぽいもの（!や/で始まる）は無視する設定（必要に応じて）
        if content.startswith("!") or content.startswith("/"):
            return

        # 保存処理
        success = await self._save_memo_to_obsidian(content, message)
        
        if success:
            await message.add_reaction("📝")
        else:
            await message.add_reaction("❌")

    async def _save_memo_to_obsidian(self, text: str, message: discord.Message) -> bool:
        """Dropbox上のデイリーノートにメモを追記する"""
        if not self.dbx:
            return False

        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        time_str = now.strftime('%H:%M')
        
        # ファイルパス
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        
        # 保存するテキストの整形
        # タイムスタンプ付きのリスト形式にする
        lines = text.split('\n')
        formatted_text = f"- {time_str} {lines[0]}"
        for line in lines[1:]:
            formatted_text += f"\n\t- {line}" # 2行目以降はインデント

        try:
            # 1. 現在のファイルを取得
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                current_content = res.content.decode('utf-8')
            except ApiError as e:
                # ファイルがない場合は新規作成
                if isinstance(e.error, dropbox.files.DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    current_content = f"# Daily Note {date_str}\n"
                else:
                    raise e

            # 2. セクションを更新 (utils.obsidian_utilsを使用)
            new_content = update_section(current_content, formatted_text, MEMO_HEADER)

            # 3. アップロード（上書き）
            await asyncio.to_thread(
                self.dbx.files_upload,
                new_content.encode('utf-8'),
                daily_note_path,
                mode=WriteMode('overwrite')
            )
            logging.info(f"Memo saved to {date_str}.md")
            return True

        except Exception as e:
            logging.error(f"MemoCog: Save Error: {e}", exc_info=True)
            return False

async def setup(bot: commands.Bot):
    if int(os.getenv("MEMO_CHANNEL_ID", 0)) == 0:
        logging.warning("MemoCog: MEMO_CHANNEL_IDが設定されていないため、ロードされません。")
        return
    await bot.add_cog(MemoCog(bot))