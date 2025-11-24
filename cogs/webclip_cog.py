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

# readabilityベースのパーサーをインポート (フォールバックとして)
from web_parser import parse_url_with_readability

# --- Google Docs連携 ---
try:
    from google_docs_handler import append_text_to_doc_async
    google_docs_enabled = True
    logging.info("WebClipCog: Google Docs連携が有効です。")
except ImportError:
    logging.warning("WebClipCog: google_docs_handler.pyが見つからないため、Google Docs連携は無効です。")
    google_docs_enabled = False
    async def append_text_to_doc_async(*args, **kwargs):
        logging.warning("Google Docs handler is not available.")
        pass
# --- ここまで ---

# --- 定数定義 ---
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

# --- リアクション定数 ---
BOT_PROCESS_TRIGGER_REACTION = '📥' 
PROCESS_START_EMOJI = '⏳'
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'
SAVE_ERROR_EMOJI = '💾'
GOOGLE_DOCS_ERROR_EMOJI = '🇬'
# --- ここまで ---


class WebClipCog(commands.Cog):
    """ウェブページの内容を取得し、Obsidianに保存するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.web_clip_channel_id = int(os.getenv("WEB_CLIP_CHANNEL_ID", 0))

        if not all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            logging.warning("WebClipCog: Dropboxの認証情報が.envファイルに設定されていません。")

    # ★ 新規追加: WebClipチャンネルへのURL投稿を検知して処理トリガーを付与
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """WebClipチャンネルにURLが投稿された場合、処理トリガー(📥)を付与する"""
        if message.author.bot:
            return
            
        if message.channel.id != self.web_clip_channel_id:
            return

        content = message.content.strip()
        if URL_REGEX.search(content):
            logging.info(f"WebClipCog: URL detected in WebClip channel. Adding trigger.")
            try:
                if not any(str(r.emoji) == BOT_PROCESS_TRIGGER_REACTION and r.me for r in message.reactions):
                    await message.add_reaction(BOT_PROCESS_TRIGGER_REACTION)
            except discord.HTTPException as e:
                logging.warning(f"WebClipCog: Failed to add trigger reaction: {e}")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Bot(自分自身)が付けた 📥 リアクションを検知して処理を開始する"""
        if payload.channel_id != self.web_clip_channel_id:
            return
            
        emoji_str = str(payload.emoji)

        if emoji_str == BOT_PROCESS_TRIGGER_REACTION:
            if payload.user_id != self.bot.user.id:
                return 
 
            channel = self.bot.get_channel(payload.channel_id)
            if not channel: return
            try:
                message = await channel.fetch_message(payload.message_id)
            except (discord.NotFound, discord.Forbidden):
                logging.warning(f"メッセージの取得に失敗しました: {payload.message_id}")
                return

            is_processed = any(r.emoji in (
                PROCESS_START_EMOJI, PROCESS_COMPLETE_EMOJI, PROCESS_ERROR_EMOJI, 
                SAVE_ERROR_EMOJI, GOOGLE_DOCS_ERROR_EMOJI
                ) and r.me for r in message.reactions)
            
            if is_processed:
                return

            logging.info(f"Bot (self) の '{BOT_PROCESS_TRIGGER_REACTION}' を検知しました。WebClip処理を開始します: {message.jump_url}")
            
            try:
                await message.remove_reaction(payload.emoji, self.bot.user)
            except discord.HTTPException:
                logging.warning(f"Bot のリアクション削除に失敗しました: {message.jump_url}")

            await self._perform_clip(message=message)


    async def _perform_clip(self, message: discord.Message):
        """Webクリップのコアロジック"""
        url = message.content.strip()
        
        obsidian_save_success = False
        gdoc_save_success = False
        error_reactions = set()
        title = "Untitled"
        content_md = ""

        try:
            await message.add_reaction(PROCESS_START_EMOJI)

            if message.embeds:
                embed_title = message.embeds[0].title
                if embed_title and embed_title != discord.Embed.Empty:
                    title = embed_title
                    logging.info(f"Title found via Discord embed: {title}")
            
            loop = asyncio.get_running_loop()
            parsed_title, content_md = await loop.run_in_executor(
                None, parse_url_with_readability, url
            )
            
            if title == "Untitled" and parsed_title and parsed_title != "No Title Found":
                title = parsed_title
                logging.info(f"Title (fallback) found via web_parser: {title}")

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
            
            dbx = None
            if self.dropbox_refresh_token:
                try:
                    dbx = dropbox.Dropbox(
                        oauth2_refresh_token=self.dropbox_refresh_token,
                        app_key=self.dropbox_app_key,
                        app_secret=self.dropbox_app_secret
                    )
                except Exception as e_dbx:
                     logging.error(f"Dropboxクライアントの初期化に失敗: {e_dbx}")
                     error_reactions.add(SAVE_ERROR_EMOJI)
            else:
                logging.error("Dropboxリフレッシュトークンがありません。")
                error_reactions.add(SAVE_ERROR_EMOJI)

            if dbx:
                try:
                    webclip_file_path = f"{self.dropbox_vault_path}/WebClips/{webclip_file_name}"
                    await asyncio.to_thread(
                        dbx.files_upload,
                        webclip_note_content.encode('utf-8'),
                        webclip_file_path,
                        mode=WriteMode('add')
                    )
                    logging.info(f"クリップ成功: {webclip_file_path}")
    
                    daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"
    
                    try:
                        _, res = await asyncio.to_thread(dbx.files_download, daily_note_path)
                        daily_note_content = res.content.decode('utf-8')
                    except ApiError as e:
                        if isinstance(e.error, dropbox.files.DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                            daily_note_content = "" 
                            logging.info(f"デイリーノート {daily_note_path} は存在しないため、新規作成します。")
                        else:
                            raise 
    
                    link_to_add = f"- [[WebClips/{webclip_file_name_for_link}|{title}]]" 
                    webclips_heading = "## WebClips"
    
                    lines = daily_note_content.split('\n')
                    try:
                        heading_index = -1
                        for i, line in enumerate(lines):
                            if line.strip().lstrip('#').strip() == webclips_heading.lstrip('#').strip():
                                heading_index = i
                                break
                        if heading_index == -1: raise ValueError("Header not found")
                        
                        insert_index = heading_index + 1
                        while insert_index < len(lines) and not lines[insert_index].strip().startswith('## '):
                            insert_index += 1
                        
                        if insert_index > heading_index + 1 and lines[insert_index - 1].strip() != "":
                            lines.insert(insert_index, "")
                            insert_index += 1
                            
                        lines.insert(insert_index, link_to_add)
                        new_daily_content = "\n".join(lines)
                    except ValueError:
                        logging.info(f"Section '{webclips_heading}' not found in daily note, appending.")
                        new_daily_content = daily_note_content.strip() + f"\n\n{webclips_heading}\n{link_to_add}\n"
                    
                    await asyncio.to_thread(
                        dbx.files_upload,
                        new_daily_content.encode('utf-8'),
                        daily_note_path,
                        mode=WriteMode('overwrite')
                    )
                    logging.info(f"デイリーノートを更新しました: {daily_note_path}")
                    obsidian_save_success = True
                
                except ApiError as e_obs_api:
                    logging.error(f"Error saving to Obsidian (Dropbox API): {e_obs_api}", exc_info=True)
                    error_reactions.add(SAVE_ERROR_EMOJI)
                except Exception as e_obs_other:
                    logging.error(f"Error saving to Obsidian (Other): {e_obs_other}", exc_info=True)
                    error_reactions.add(SAVE_ERROR_EMOJI)
            
            if google_docs_enabled:
                gdoc_text_to_append = ""
                gdoc_source_type = "WebClip Error"

                if content_md: 
                    gdoc_text_to_append = content_md
                    gdoc_source_type = "WebClip Content"
                elif url: 
                    gdoc_text_to_append = "(本文の抽出に失敗しました)"
                    gdoc_source_type = "WebClip URL (Content Failed)"

                if gdoc_text_to_append:
                    try:
                        await append_text_to_doc_async(
                            text_to_append=gdoc_text_to_append,
                            source_type=gdoc_source_type,
                            url=url,
                            title=title
                        )
                        gdoc_save_success = True
                        logging.info(f"Data ({gdoc_source_type}) sent to Google Docs for {url}")
                    except Exception as e_gdoc:
                        logging.error(f"Failed to send data to Google Docs for {url}: {e_gdoc}", exc_info=True)
                        error_reactions.add(GOOGLE_DOCS_ERROR_EMOJI)
            
            if obsidian_save_success:
                await message.add_reaction(PROCESS_COMPLETE_EMOJI)
                if error_reactions:
                    for reaction in error_reactions:
                        try: await message.add_reaction(reaction)
                        except discord.HTTPException: pass
            else:
                final_reactions = error_reactions if error_reactions else {PROCESS_ERROR_EMOJI}
                for reaction in final_reactions:
                    try: await message.add_reaction(reaction)
                    except discord.HTTPException: pass

        except Exception as e:
            logging.error(f"Webクリップ処理中にエラー: {e}", exc_info=True)
            try: await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass
        finally:
            try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except discord.HTTPException: pass

    @app_commands.command(name="clip", description="URLをObsidianにクリップします。")
    @app_commands.describe(url="クリップしたいページのURL")
    async def clip(self, interaction: discord.Interaction, url: str):
        await interaction.response.defer(ephemeral=False, thinking=True)
        message_proxy = await interaction.original_response()

        class TempMessage:
             def __init__(self, proxy):
                 self.id = proxy.id
                 self.reactions = []
                 self.channel = proxy.channel
                 self.jump_url = proxy.jump_url
                 self._proxy = proxy
                 self.content = proxy.content
                 self.embeds = []
             async def add_reaction(self, emoji):
                 try: await self._proxy.add_reaction(emoji)
                 except: pass
             async def remove_reaction(self, emoji, user):
                 try: await self._proxy.remove_reaction(emoji, user)
                 except: pass

        temp_msg_obj = TempMessage(message_proxy)
        temp_msg_obj.content = url 
        await self._perform_clip(message=temp_msg_obj)


async def setup(bot: commands.Bot):
    await bot.add_cog(WebClipCog(bot))