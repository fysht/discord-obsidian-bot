import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import asyncio
import dropbox
from dropbox.files import WriteMode
import datetime
import zoneinfo

# readabilityベースのパーサーをインポート
from web_parser import parse_url_with_readability

# --- 定数定義 ---
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

class WebClipCog(commands.Cog):
    """ウェブページの内容を取得し、Obsidianに保存するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # .envファイルからDropboxの認証情報を読み込む
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.web_clip_channel_id = int(os.getenv("WEB_CLIP_CHANNEL_ID", 0))

        if not all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            logging.warning("WebClipCog: Dropboxの認証情報が.envファイルに設定されていません。")

    async def _perform_clip(self, url: str, message: discord.Message):
        """Webクリップのコアロジック"""
        try:
            await message.add_reaction("⏳")
            
            loop = asyncio.get_running_loop()
            title, content_md = await loop.run_in_executor(
                None, parse_url_with_readability, url
            )
            
            # ファイル名として使えない文字を削除・置換
            safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
            if not safe_title:
                safe_title = "Untitled"
            
            # ファイル名に日付と時刻を追加して、一意性を確保する
            now = datetime.datetime.now(JST)
            timestamp = now.strftime('%Y%m%d-%H%M%S')
            file_name = f"{safe_title}-{timestamp}.md"

            # ページタイトルとURLをファイルの先頭に追加する形式
            final_content = f"# {title}\n\n**Source:** <{url}>\n\n---\n\n{content_md}"
            
            # 保存先のファイルパスを定義
            file_path = f"{self.dropbox_vault_path}/WebClips/{file_name}"
            
            with dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key,
                app_secret=self.dropbox_app_secret
            ) as dbx:
                dbx.files_upload(
                    final_content.encode('utf-8'),
                    file_path,
                    mode=WriteMode('add')
                )

            logging.info(f"クリップ成功: {file_path}")
            await message.add_reaction("✅")

        except Exception as e:
            logging.error(f"Webクリップ処理中にエラー: {e}", exc_info=True)
            await message.add_reaction("❌")
        finally:
            await message.remove_reaction("⏳", self.bot.user)
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.channel.id != self.web_clip_channel_id:
            return

        content = message.content.strip()
        if content.startswith('http') and URL_REGEX.match(content):
            url = content
            await self._perform_clip(url=url, message=message)

    @app_commands.command(name="clip", description="URLをObsidianにクリップします。")
    @app_commands.describe(url="クリップしたいページのURL")
    async def clip(self, interaction: discord.Interaction, url: str):
        await interaction.response.send_message(f"`{url}` のクリップ処理を開始します...", ephemeral=True)
        message = await interaction.original_response()
        await self._perform_clip(url=url, message=message)

async def setup(bot: commands.Bot):
    await bot.add_cog(WebClipCog(bot))