import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
from urllib.parse import urlparse
import datetime
import zoneinfo
import dropbox
from dropbox.files import WriteMode
import asyncio

# 新しく作成したパーサーをインポート
from web_parser import parse_url_advanced 

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
# メッセージからURLを検出するための正規表現
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')

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

        # 認証情報が不足している場合は警告を出す
        if not all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            logging.warning("WebClipCog: Dropboxの認証情報が.envファイルに設定されていません。")

    async def _perform_clip(self, url: str, message: discord.Message):
        """Webクリップのコアロジック"""
        try:
            # 処理中であることをユーザーに知らせる
            await message.add_reaction("⏳")
            
            # 新しいパーサーを別スレッドで実行
            loop = asyncio.get_running_loop()
            # parse_url_advancedは重い処理なので、Botの非同期処理をブロックしないようにExecutorで実行
            fetched_title, fetched_content = await loop.run_in_executor(
                None, parse_url_advanced, url
            )
            
            now = datetime.datetime.now(JST)
            today_str = now.strftime('%Y-%m-%d')
            time_str = now.strftime('%H%M%S')
            
            # URLからドメイン名を取得してファイル名に利用
            try:
                domain = urlparse(url).netloc
            except Exception:
                domain = "unknown"

            # ページの取得に失敗した場合は、ブックマークとして最低限の情報を保存
            if fetched_title is None:
                logging.warning(f"ページの取得に失敗したため、ブックマークとして保存します: {url}")
                file_name = f"Bookmark-{domain}-{today_str}-{time_str}"
                markdown_content = (
                    f"# Web Clip (Bookmark)\n\n"
                    f"- URL: <{url}>\n"
                    f"- 作成日: {today_str}\n"
                    f"- 関連: [[{today_str}]]\n\n"
                    f"---\n\n"
                    f"（ページの読み込みに失敗しました）"
                )
            else:
                 # 成功した場合は、タイトルと取得した本文を整形して保存
                 file_name = f"WebClip-{domain}-{today_str}-{time_str}"
                 markdown_content = (
                    f"# {fetched_title}\n\n"
                    f"- URL: <{url}>\n"
                    f"- 作成日: {today_str}\n"
                    f"- 関連: [[{today_str}]]\n\n"
                    f"---\n\n"
                    f"{fetched_content}"
                )

            # 保存先のファイルパスを定義
            file_path = f"{self.dropbox_vault_path}/WebClips/{file_name}.md"
            
            # Dropboxに接続してファイルをアップロード
            with dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key,
                app_secret=self.dropbox_app_secret
            ) as dbx:
                dbx.files_upload(
                    markdown_content.encode('utf-8'),
                    file_path,
                    mode=WriteMode('add') # 同じファイル名があっても上書きしない
                )

            logging.info(f"クリップ成功: {file_path}")
            await message.add_reaction("✅") # 成功をユーザーに知らせる

        except Exception as e:
            logging.error(f"Webクリップ処理中にエラー: {e}", exc_info=True)
            await message.add_reaction("❌") # 失敗をユーザーに知らせる
        finally:
            # 成功・失敗に関わらず、処理中のリアクションは削除
            await message.remove_reaction("⏳", self.bot.user)
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """指定されたチャンネルのメッセージを監視し、URLがあればクリップ処理を実行"""
        # ボット自身のメッセージや、指定外のチャンネルは無視
        if message.author.bot or message.channel.id != self.web_clip_channel_id:
            return

        # メッセージからURLを正規表現で探す
        match = URL_REGEX.search(message.content)
        if match:
            url = match.group(0)
            # クリップ処理を呼び出す
            await self._perform_clip(url=url, message=message)

    @app_commands.command(name="clip", description="URLをObsidianにクリップします。")
    @app_commands.describe(url="クリップしたいページのURL")
    async def clip(self, interaction: discord.Interaction, url: str):
        """スラッシュコマンドによる手動実行"""
        await interaction.response.send_message(f"`{url}` のクリップ処理を開始します...", ephemeral=True)
        message = await interaction.original_response()
        await self._perform_clip(url=url, message=message)

async def setup(bot: commands.Bot):
    await bot.add_cog(WebClipCog(bot))