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

# readabilityベースのパーサーをインポート
from web_parser import parse_url_with_readability

# --- 定数定義 ---
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
SECTION_ORDER = [
    "## WebClips",
    "## YouTube Summaries",
    "## AI Logs",
    "## Zero-Second Thinking",
    "## Memo"
]

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

    def _update_daily_note_with_ordered_section(self, current_content: str, link_to_add: str, section_header: str) -> str:
        """定義された順序に基づいてデイリーノートのコンテンツを更新する"""
        lines = current_content.split('\n')
        
        # セクションが既に存在するか確認
        try:
            header_index = lines.index(section_header)
            insert_index = header_index + 1
            while insert_index < len(lines) and (lines[insert_index].strip().startswith('- ') or not lines[insert_index].strip()):
                insert_index += 1
            lines.insert(insert_index, link_to_add)
            return "\n".join(lines)
        except ValueError:
            # セクションが存在しない場合、正しい位置に新規作成
            existing_sections = {line.strip(): i for i, line in enumerate(lines) if line.strip() in SECTION_ORDER}
            
            insert_after_index = -1
            new_section_order_index = SECTION_ORDER.index(section_header)
            for i in range(new_section_order_index - 1, -1, -1):
                preceding_header = SECTION_ORDER[i]
                if preceding_header in existing_sections:
                    header_line_index = existing_sections[preceding_header]
                    insert_after_index = header_line_index + 1
                    while insert_after_index < len(lines) and not lines[insert_after_index].strip().startswith('## '):
                        insert_after_index += 1
                    break
            
            if insert_after_index != -1:
                lines.insert(insert_after_index, f"\n{section_header}\n{link_to_add}")
                return "\n".join(lines)

            insert_before_index = -1
            for i in range(new_section_order_index + 1, len(SECTION_ORDER)):
                following_header = SECTION_ORDER[i]
                if following_header in existing_sections:
                    insert_before_index = existing_sections[following_header]
                    break
            
            if insert_before_index != -1:
                lines.insert(insert_before_index, f"{section_header}\n{link_to_add}\n")
                return "\n".join(lines)

            if current_content.strip():
                 lines.append("")
            lines.append(section_header)
            lines.append(link_to_add)
            return "\n".join(lines)

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
                daily_note_content = ""
                try:
                    _, res = dbx.files_download(daily_note_path)
                    daily_note_content = res.content.decode('utf-8')
                except ApiError as e:
                    if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                        logging.info(f"デイリーノート {daily_note_path} は存在しないため、新規作成します。")
                    else:
                        raise

                link_to_add = f"- [[WebClips/{webclip_file_name_for_link}]]"
                webclips_heading = "## WebClips"
                
                new_daily_content = self._update_daily_note_with_ordered_section(
                    daily_note_content, link_to_add, webclips_heading
                )

                dbx.files_upload(
                    new_daily_content.encode('utf-8'),
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