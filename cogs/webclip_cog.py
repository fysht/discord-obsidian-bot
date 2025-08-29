import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import asyncio
import dropbox
from dropbox.files import WriteMode
from dropbox.exceptions import ApiError
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

            safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
            if not safe_title:
                safe_title = "Untitled"

            now = datetime.datetime.now(JST)
            timestamp = now.strftime('%Y%m%d%H%M%S')
            daily_note_date = now.strftime('%Y-%m-%d')

            webclip_file_name = f"{timestamp}-{safe_title}.md"
            webclip_file_name_for_link = webclip_file_name.replace('.md', '')

            webclip_note_content = (
                f"# {title}\n\n"
                f"- **Source:** <{url}>\n\n"
                f"---\n\n"
                f"[[{daily_note_date}]]\n\n"
                f"{content_md}"
            )

            with dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key,
                app_secret=self.dropbox_app_secret
            ) as dbx:
                webclip_file_path = f"{self.dropbox_vault_path}/WebClips/{webclip_file_name}"
                dbx.files_upload(
                    webclip_note_content.encode('utf-8'),
                    webclip_file_path,
                    mode=WriteMode('add')
                )
                logging.info(f"クリップ成功: {webclip_file_path}")

                daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"

                try:
                    _, res = dbx.files_download(daily_note_path)
                    daily_note_content = res.content.decode('utf-8')
                except ApiError as e:
                    if isinstance(e.error, dropbox.files.DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                        daily_note_content = ""
                        logging.info(f"デイリーノート {daily_note_path} は存在しないため、新規作成します。")
                    else:
                        raise

                link_to_add = f"- [[{webclip_file_name_for_link}]]"
                webclips_heading = "## WebClips"

                lines = daily_note_content.split('\n')
                try:
                    heading_index = lines.index(webclips_heading)
                    insert_index = heading_index + 1
                    while insert_index < len(lines):
                        # セクション内のリストの末尾を探す
                        if lines[insert_index].strip().startswith('- ') or lines[insert_index].strip() == "":
                            insert_index += 1
                        else:
                            break
                    lines.insert(insert_index, link_to_add)
                    daily_note_content = "\n".join(lines)
                except ValueError:
                    # 「## WebClips」セクションが存在しない場合は、ファイルの先頭に新規作成する
                    new_section = f"{webclips_heading}\n{link_to_add}\n"
                    # 既存のコンテンツとの間に空行を1行挟む
                    if daily_note_content.strip():
                        daily_note_content = new_section + "\n" + daily_note_content
                    else:
                        daily_note_content = new_section

                dbx.files_upload(
                    daily_note_content.encode('utf-8'),
                    daily_note_path,
                    mode=WriteMode('overwrite')
                )
                logging.info(f"デイリーノートを更新しました: {daily_note_path}")

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