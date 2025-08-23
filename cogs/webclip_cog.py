import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
from datetime import datetime
import dropbox
from dropbox.files import WriteMode
from web_parser import parse_url 
import zoneinfo
import re

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
# URLを検出するための簡易的な正規表現
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
        # 環境変数からWebクリップ用チャンネルのIDを取得
        self.web_clip_channel_id = int(os.getenv("WEB_CLIP_CHANNEL_ID", 0))

    async def _perform_clip(self, url: str, interaction: discord.Interaction | None = None, message: discord.Message | None = None):
        """Webクリップのコアロジック。コマンドとon_messageの両方から呼ばれる。"""
        try:
            logging.info(f"クリップを開始します: {url}")
            # web_parser.pyの関数を呼び出してページ内容を取得
            title, content = await parse_url(url)

            if not title or not content:
                error_msg = "エラー: ページのタイトルまたは本文を取得できませんでした。"
                if interaction:
                    await interaction.followup.send(error_msg, ephemeral=True)
                elif message:
                    await message.channel.send(f"{message.author.mention} {error_msg}")
                return

            # Obsidianに保存するMarkdownコンテンツを作成
            now = datetime.now(JST)
            timestamp = now.strftime('%Y-%m-%d %H:%M:%S')
            file_timestamp = now.strftime('%Y-%m-%d-%H-%M-%S')
            
            markdown_content = (
                f"# {title}\n\n"
                f"- **URL**: {url}\n"
                f"- **Clipped at**: {timestamp}\n\n"
                f"---\n\n"
                f"{content}"
            )

            # Dropboxにファイルをアップロード
            file_path = f"{self.dropbox_vault_path}/WebClips/{file_timestamp}.md"
            
            with dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key,
                app_secret=self.dropbox_app_secret
            ) as dbx:
                dbx.files_upload(
                    markdown_content.encode('utf-8'),
                    file_path,
                    mode=WriteMode('add') # 新規ファイルとして追加
                )

            logging.info(f"クリップが成功しました: {file_path}")
            
            # 成功を通知
            if interaction:
                embed = discord.Embed(
                    title="✅ Webクリップ成功",
                    description=f"**[{title}]({url})** をObsidianに保存しました。",
                    color=discord.Color.green()
                )
                embed.add_field(name="保存先", value=f"`{file_path}`")
                await interaction.followup.send(embed=embed, ephemeral=True)
            elif message:
                await message.add_reaction("✅")


        except Exception as e:
            logging.error(f"Webクリップ処理中にエラーが発生しました: {e}", exc_info=True)
            if interaction:
                await interaction.followup.send(f"🤖 エラーが発生しました: {e}", ephemeral=True)
            elif message:
                await message.add_reaction("❌")
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """メッセージ投稿を監視し、特定チャンネルのURLを自動でクリップする"""
        # Bot自身のメッセージは無視
        if message.author.bot:
            return
            
        # 指定されたWebクリップ用チャンネルでなければ無視
        if message.channel.id != self.web_clip_channel_id:
            return

        # メッセージからURLを検索
        match = URL_REGEX.search(message.content)
        if match:
            url = match.group(0)
            await message.add_reaction("⏳") # 処理中のリアクション
            await self._perform_clip(url=url, message=message)
            await message.remove_reaction("⏳", self.bot.user) # 処理中リアクションを削除

    @app_commands.command(name="clip", description="指定したURLのウェブページをObsidianにクリップします。")
    @app_commands.describe(url="クリップしたいウェブページのURL")
    async def clip(self, interaction: discord.Interaction, url: str):
        """スラッシュコマンド '/clip' の処理"""
        await interaction.response.defer(ephemeral=True) # 応答を保留
        await self._perform_clip(url=url, interaction=interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(WebClipCog(bot))