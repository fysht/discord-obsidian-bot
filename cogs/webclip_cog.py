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
import aiohttp

# web_parser (既存のまま)
from web_parser import parse_url_with_readability

# 共通関数をインポート
try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.warning("WebClipCog: utils/obsidian_utils.pyが見つかりません。")
    def update_section(content, text, header): return f"{content}\n\n{header}\n{text}"

# --- 定数定義 ---
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
WEBCLIP_SECTION = "## WebClips"

# --- リアクション定数 ---
BOT_PROCESS_TRIGGER_REACTION = '📥' 
PROCESS_START_EMOJI = '⏳'
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'
SAVE_ERROR_EMOJI = '💾'

class WebClipCog(commands.Cog):
    """ウェブページの内容を取得し、Obsidianに保存するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.web_clip_channel_id = int(os.getenv("WEB_CLIP_CHANNEL_ID", 0))
        
        self.dbx = None
        if all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            try:
                self.dbx = dropbox.Dropbox(
                    oauth2_refresh_token=self.dropbox_refresh_token,
                    app_key=self.dropbox_app_key,
                    app_secret=self.dropbox_app_secret
                )
            except Exception as e:
                logging.error(f"WebClipCog: Dropbox Init Error: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """WebClipチャンネルにURLが投稿された場合、処理トリガー(📥)を付与する"""
        if message.author.bot: return
        if message.channel.id != self.web_clip_channel_id: return

        content = message.content.strip()
        if URL_REGEX.search(content):
            try:
                if not any(str(r.emoji) == BOT_PROCESS_TRIGGER_REACTION and r.me for r in message.reactions):
                    await message.add_reaction(BOT_PROCESS_TRIGGER_REACTION)
            except discord.HTTPException: pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Bot(自分自身)が付けた 📥 リアクションを検知して処理を開始する"""
        if payload.channel_id != self.web_clip_channel_id: return
        if str(payload.emoji) != BOT_PROCESS_TRIGGER_REACTION: return
        if payload.user_id != self.bot.user.id: return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return
        try: message = await channel.fetch_message(payload.message_id)
        except: return

        is_processed = any(r.emoji in (PROCESS_START_EMOJI, PROCESS_COMPLETE_EMOJI, PROCESS_ERROR_EMOJI, SAVE_ERROR_EMOJI) and r.me for r in message.reactions)
        if is_processed: return

        try: await message.remove_reaction(payload.emoji, self.bot.user)
        except: pass

        await self._perform_clip(message=message)

    async def _perform_clip(self, message: discord.Message):
        """Webクリップのコアロジック"""
        url = message.content.strip()
        obsidian_save_success = False
        error_reactions = set()
        title = "Untitled"
        content_md = ""

        try:
            await message.add_reaction(PROCESS_START_EMOJI)

            # 1. タイトル取得 (Embed優先)
            if message.embeds and message.embeds[0].title:
                title = message.embeds[0].title
            
            # 2. コンテンツ解析 (readability)
            loop = asyncio.get_running_loop()
            parsed_title, content_md = await loop.run_in_executor(None, parse_url_with_readability, url)
            
            if title == "Untitled" and parsed_title and parsed_title != "No Title Found":
                title = parsed_title

            safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
            if not safe_title: safe_title = "Untitled"

            now = datetime.datetime.now(JST)
            timestamp = now.strftime('%Y%m%d%H%M%S')
            daily_note_date = now.strftime('%Y-%m-%d')

            # 3. Webクリップファイルの作成
            webclip_file_name = f"{timestamp}-{safe_title}.md"
            webclip_file_name_for_link = webclip_file_name.replace('.md', '')

            webclip_note_content = (
                f"# {title}\n\n"
                f"- **Source:** <{url}>\n\n"
                f"---\n\n"
                f"[[{daily_note_date}]]\n\n"
                f"{content_md}"
            )

            if self.dbx:
                try:
                    # 個別ノート保存
                    webclip_file_path = f"{self.dropbox_vault_path}/WebClips/{webclip_file_name}"
                    await asyncio.to_thread(
                        self.dbx.files_upload,
                        webclip_note_content.encode('utf-8'),
                        webclip_file_path,
                        mode=WriteMode('add')
                    )

                    # デイリーノート更新
                    daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"
                    try:
                        _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                        daily_note_content = res.content.decode('utf-8')
                    except ApiError as e:
                        if isinstance(e.error, dropbox.files.DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                            daily_note_content = f"# Daily Note {daily_note_date}\n"
                        else:
                            raise 
    
                    # リンクを作成して挿入
                    link_to_add = f"- [[WebClips/{webclip_file_name_for_link}|{title}]]"
                    new_daily_content = update_section(daily_note_content, link_to_add, WEBCLIP_SECTION)
                    
                    await asyncio.to_thread(
                        self.dbx.files_upload,
                        new_daily_content.encode('utf-8'),
                        daily_note_path,
                        mode=WriteMode('overwrite')
                    )
                    
                    obsidian_save_success = True
                
                except Exception as e:
                    logging.error(f"Error saving to Obsidian: {e}", exc_info=True)
                    error_reactions.add(SAVE_ERROR_EMOJI)
            else:
                logging.error("Dropbox client not initialized.")
                error_reactions.add(SAVE_ERROR_EMOJI)
            
            if obsidian_save_success:
                await message.add_reaction(PROCESS_COMPLETE_EMOJI)
            else:
                for r in error_reactions or [PROCESS_ERROR_EMOJI]:
                    try: await message.add_reaction(r)
                    except: pass

        except Exception as e:
            logging.error(f"WebClip Error: {e}", exc_info=True)
            try: await message.add_reaction(PROCESS_ERROR_EMOJI)
            except: pass
        finally:
            try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except: pass

    @app_commands.command(name="clip", description="URLをObsidianにクリップします。")
    @app_commands.describe(url="クリップしたいページのURL")
    async def clip(self, interaction: discord.Interaction, url: str):
        await interaction.response.defer(ephemeral=False, thinking=True)
        # 簡易的なメッセージプロキシオブジェクトを作成してロジックを再利用
        class TempMessage:
             def __init__(self, original):
                 self.id = original.id
                 self.channel = original.channel
                 self.content = url
                 self.embeds = []
                 self.reactions = []
                 self._original = original
             async def add_reaction(self, emoji): 
                 try: await self._original.add_reaction(emoji)
                 except: pass
             async def remove_reaction(self, emoji, user):
                 try: await self._original.remove_reaction(emoji, user)
                 except: pass
        
        await self._perform_clip(message=TempMessage(await interaction.original_response()))

async def setup(bot: commands.Bot):
    await bot.add_cog(WebClipCog(bot))