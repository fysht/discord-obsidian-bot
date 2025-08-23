import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import datetime
import zoneinfo
import dropbox
from dropbox.files import WriteMode
from web_parser import parse_url

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')

class WebClipCog(commands.Cog):
    """ウェブページの内容を取得し、Obsidianに保存するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # .envファイルから各種設定を読み込む
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.web_clip_channel_id = int(os.getenv("WEB_CLIP_CHANNEL_ID", 0))

        # Dropbox設定が不完全な場合は警告を出す
        if not all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            logging.warning("WebClipCog: Dropboxの認証情報が.envファイルに設定されていません。")

    async def _perform_clip(self, url: str, message: discord.Message):
        """Webクリップのコアロジック"""
        try:
            await message.add_reaction("⏳")
            
            title, content = await parse_url(url)

            if not title or not content:
                await message.channel.send(f"{message.author.mention} ページのタイトルまたは本文を取得できませんでした。")
                return

            now = datetime.datetime.now(JST)
            today_str = now.strftime('%Y-%m-%d')
            
            # ファイル名に使えない文字を削除・置換
            safe_title = re.sub(r'[\\|/|:|*|?|"|<|>|\|]', '-', title)
            file_name = f"WebClip - {safe_title[:50]} - {now.strftime('%Y%m%d%H%M%S')}"
            
            markdown_content = (
                f"# {title}\n\n"
                f"- **URL**: <{url}>\n"
                f"- **Clipped at**: {now.strftime('%Y-%m-%d %H:%M')}\n"
                f"- **関連**: [[{today_str}]]\n\n"
                f"---\n\n"
                f"{content}"
            )

            file_path = f"{self.dropbox_vault_path}/WebClips/{file_name}.md"
            
            with dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key,
                app_secret=self.dropbox_app_secret
            ) as dbx:
                dbx.files_upload(
                    markdown_content.encode('utf-8'),
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
        """指定チャンネルのURLを自動クリップ"""
        if message.author.bot or message.channel.id != self.web_clip_channel_id:
            return

        match = URL_REGEX.search(message.content)
        if match:
            url = match.group(0)
            await self._perform_clip(url=url, message=message)

    @app_commands.command(name="clip", description="URLをObsidianにクリップします。")
    @app_commands.describe(url="クリップしたいページのURL")
    async def clip(self, interaction: discord.Interaction, url: str):
        """スラッシュコマンド /clip の処理"""
        # ephemeral=True で本人にのみ表示される応答
        await interaction.response.send_message(f"`{url}` のクリップ処理を開始します...", ephemeral=True)
        # interactionからmessageオブジェクトを取得して共通処理に渡す
        message = await interaction.original_response()
        await self._perform_clip(url=url, message=message)

async def setup(bot: commands.Bot):
    await bot.add_cog(WebClipCog(bot))