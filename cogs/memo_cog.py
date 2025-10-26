import os
import discord
from discord.ext import commands
import asyncio
import logging
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError # ApiError をインポート
from datetime import datetime, timezone, timedelta
import json
import re
import aiohttp # YouTubeタイトル取得用にインポート
from obsidian_handler import add_memo_async
from utils.obsidian_utils import update_section
from web_parser import parse_url_with_readability # web_parserをインポート

# --- Constants ---
try:
    import zoneinfo
    JST = zoneinfo.ZoneInfo("Asia/Tokyo")
except ImportError:
    # Python 3.8以前またはzoneinfo未インストールの場合のフォールバック
    JST = timezone(timedelta(hours=+9), "JST")


# Use channel ID from env var
MEMO_CHANNEL_ID = int(os.getenv("MEMO_CHANNEL_ID", 0))

# Reaction Emojis
TITLE_LINK_EMOJI = '🇹' # T for Title
CLIP_SUMMARY_EMOJI = '📄' # Page for Clip/Summary
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'
PROCESS_START_EMOJI = '⏳' # 処理中を示す絵文字
YOUTUBE_REACTION_EMOJI = '▶️' # YouTube URL用に一時的につけるリアクション

# URL Regex
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')
# YouTube Regex
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})')

# Daily Note Section Header
MEMO_SECTION_HEADER = "## Memo"
WEBCLIPS_SECTION_HEADER = "## WebClips" # Webクリップ保存用

# Cog Class
class MemoCog(commands.Cog):
    """Discordの#memoチャンネルを監視し、テキストメモまたはURL処理を行うCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Store message IDs that are waiting for a reaction
        # >>>>>>>>>>>>>>>>>> MODIFICATION #1 START <<<<<<<<<<<<<<<<<<
        # Store URL info as a dictionary including is_youtube flag
        self.pending_url_messages = {} # {message_id: {"url": url, "is_youtube": bool}}
        # >>>>>>>>>>>>>>>>>> MODIFICATION #1 END <<<<<<<<<<<<<<<<<<
        self.dbx = None # Initialize dbx client for saving links/clips
        self._initialize_dropbox()

    def _initialize_dropbox(self):
        """Initialize Dropbox client from environment variables."""
        dbx_refresh = os.getenv("DROPBOX_REFRESH_TOKEN")
        dbx_key = os.getenv("DROPBOX_APP_KEY")
        dbx_secret = os.getenv("DROPBOX_APP_SECRET")
        if all([dbx_refresh, dbx_key, dbx_secret]):
            try:
                self.dbx = dropbox.Dropbox(
                    oauth2_refresh_token=dbx_refresh,
                    app_key=dbx_key,
                    app_secret=dbx_secret,
                    timeout=60 # 必要に応じてタイムアウトを調整
                )
                self.dbx.users_get_current_account() # Test connection
                logging.info("MemoCog: Dropbox client initialized successfully.")
            except Exception as e:
                logging.error(f"MemoCog: Failed to initialize Dropbox client: {e}")
                self.dbx = None
        else:
            logging.warning("MemoCog: Dropbox credentials missing. Saving title/link/clips to Obsidian will fail.")
            self.dbx = None
        # Store vault path regardless of client initialization for path construction
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")


    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """#memo チャンネルに投稿されたメッセージを処理"""
        if message.author.bot or message.channel.id != MEMO_CHANNEL_ID:
            return

        content = message.content.strip()
        if not content:
            return

        # Check for URL
        url_match = URL_REGEX.search(content)
        if url_match:
            url = url_match.group(0)
            logging.info(f"URL detected in message {message.id}: {url}")

            # >>>>>>>>>>>>>>>>>> MODIFICATION #1 START <<<<<<<<<<<<<<<<<<
            # --- Check if it's a YouTube URL immediately ---
            youtube_match = YOUTUBE_URL_REGEX.search(url)
            if youtube_match:
                logging.info(f"YouTube URL detected in message {message.id}: {url}")
                # Store with is_youtube flag set to True
                self.pending_url_messages[message.id] = {"url": url, "is_youtube": True}
            else:
                # Store with is_youtube flag set to False
                self.pending_url_messages[message.id] = {"url": url, "is_youtube": False}
            # --------------------------------------------------
            # >>>>>>>>>>>>>>>>>> MODIFICATION #1 END <<<<<<<<<<<<<<<<<<

            try:
                await message.add_reaction(TITLE_LINK_EMOJI)
                await message.add_reaction(CLIP_SUMMARY_EMOJI)
            except discord.Forbidden:
                logging.error(f"Missing permissions to add reactions in channel {message.channel.name}")
            except discord.HTTPException as e:
                logging.error(f"Failed to add reactions to message {message.id}: {e}")
        else:
            # Not a URL, treat as a regular memo
            logging.info(f"Text memo detected in message {message.id}. Saving via obsidian_handler.")
            try:
                # Use add_memo_async for non-blocking save to pending file
                await add_memo_async(
                    content=content,
                    author=str(message.author),
                    created_at=message.created_at.isoformat(),
                    message_id=message.id,
                    context="General", # Or derive context if needed
                    category="Memo"
                )
                await message.add_reaction("📝")
                async def remove_temp_reaction(msg, emoji):
                    await asyncio.sleep(15)
                    try:
                        await msg.remove_reaction(emoji, self.bot.user)
                    except discord.HTTPException: pass
                asyncio.create_task(remove_temp_reaction(message, "📝"))

            except Exception as e:
                logging.error(f"Failed to save text memo using add_memo_async: {e}", exc_info=True)
                await message.add_reaction(PROCESS_ERROR_EMOJI)


    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """URLメッセージへのリアクションを処理"""
        if payload.user_id == self.bot.user.id or payload.channel_id != MEMO_CHANNEL_ID:
            return
        if payload.message_id not in self.pending_url_messages:
            return
        emoji = str(payload.emoji)
        if emoji not in [TITLE_LINK_EMOJI, CLIP_SUMMARY_EMOJI]:
            return

        # >>>>>>>>>>>>>>>>>> MODIFICATION #1 START <<<<<<<<<<<<<<<<<<
        # Get the URL info dictionary
        info = self.pending_url_messages.pop(payload.message_id, None)
        if not info:
            logging.warning(f"Reaction {emoji} on message {payload.message_id} but info not found in pending list.")
            return
        url = info["url"]
        is_youtube = info.get("is_youtube", False) # Get the pre-determined flag
        # >>>>>>>>>>>>>>>>>> MODIFICATION #1 END <<<<<<<<<<<<<<<<<<

        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return
        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            logging.error(f"Failed to fetch message {payload.message_id} for reaction processing.")
            return

        is_processing = False
        for reaction in message.reactions:
            if reaction.emoji == PROCESS_START_EMOJI and reaction.me:
                is_processing = True
                break
        if is_processing:
            logging.warning(f"Message {message.id} is already being processed. Ignoring reaction {emoji}.")
            return

        try:
            await message.remove_reaction(TITLE_LINK_EMOJI, self.bot.user)
            await message.remove_reaction(CLIP_SUMMARY_EMOJI, self.bot.user)
        except discord.HTTPException:
            logging.warning(f"Failed to remove initial reactions from message {message.id}")

        try:
            if emoji == TITLE_LINK_EMOJI:
                logging.info(f"Processing '{TITLE_LINK_EMOJI}' reaction for message {message.id} (URL: {url})")
                await self.save_title_and_link(message, url, is_youtube) # Pass is_youtube flag

            elif emoji == CLIP_SUMMARY_EMOJI:
                logging.info(f"Processing '{CLIP_SUMMARY_EMOJI}' reaction for message {message.id} (URL: {url})")
                # Use the pre-determined is_youtube flag for branching
                await self.trigger_clip_or_summary(message, url, is_youtube)

        except Exception as e:
             logging.error(f"[Reaction Processing Error] Error processing reaction {emoji} for message {message.id}: {e}", exc_info=True)
             try:
                 await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
             except discord.HTTPException: pass
             try:
                 await message.add_reaction(PROCESS_ERROR_EMOJI)
             except discord.HTTPException: pass


    async def save_title_and_link(self, message: discord.Message, url: str, is_youtube: bool): # Added is_youtube parameter
        """URLのタイトルを取得し、デイリーノートのMemoセクションにリンクとして保存する"""
        if not self.dbx:
            logging.error("Cannot save title/link: Dropbox client is not initialized.")
            await message.add_reaction(PROCESS_ERROR_EMOJI)
            return

        await message.add_reaction(PROCESS_START_EMOJI)
        title = "Untitled"

        try:
            # Use the pre-determined is_youtube flag
            if is_youtube:
                youtube_match = YOUTUBE_URL_REGEX.search(url) # Search again to get video_id
                video_id = youtube_match.group(1) if youtube_match else None
                if video_id:
                    logging.info(f"Fetching title for YouTube video ID: {video_id}")
                    try:
                        async with aiohttp.ClientSession() as session:
                             oembed_url = f"https://www.youtube.com/oembed?url=http://www.youtube.com/watch?v={video_id}&format=json"
                             async with session.get(oembed_url, timeout=10) as response:
                                 if response.status == 200:
                                     data = await response.json()
                                     title = data.get("title", f"YouTube_{video_id}")
                                     logging.info(f"YouTube title fetched: {title}")
                                 else:
                                     logging.warning(f"oEmbed failed for {video_id}: Status {response.status}")
                                     title = f"YouTube_{video_id}"
                    except asyncio.TimeoutError:
                        logging.error("Timeout fetching YouTube title via oEmbed.")
                        title = f"YouTube_{video_id}"
                    except Exception as e_yt_title:
                         logging.error(f"Error fetching YouTube title via oEmbed: {e_yt_title}", exc_info=True)
                         title = f"YouTube_{video_id}"
                else:
                    logging.warning(f"Could not extract video ID from YouTube URL: {url}")
                    title = "YouTube Video (Unknown ID)"
            else:
                # General Web URL
                logging.info(f"Fetching title for web URL: {url}")
                loop = asyncio.get_running_loop()
                fetched_title, _ = await loop.run_in_executor(
                    None, parse_url_with_readability, url
                )
                if fetched_title and fetched_title != "No Title Found":
                    title = fetched_title
                    logging.info(f"Web title fetched: {title}")
                else:
                    logging.warning(f"Failed to fetch title using readability for {url}. Using URL as title.")
                    title = url

            now = datetime.now(JST)
            date_str = now.strftime('%Y-%m-%d')
            time_str = now.strftime('%H:%M')

            link_text = f"- {time_str} [{title}]({url})"
            logging.debug(f"Formatted link text: {link_text}")

            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
            current_content = ""

            try:
                logging.info(f"Downloading daily note: {daily_note_path}")
                _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                current_content = res.content.decode('utf-8')
                logging.info(f"Daily note downloaded successfully.")
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    current_content = f"# {date_str}\n"
                    logging.info(f"Creating new daily note: {daily_note_path}")
                else:
                    logging.error(f"Dropbox download error: {e}", exc_info=True)
                    raise

            logging.info(f"Updating section '{MEMO_SECTION_HEADER}' in daily note.")
            new_content = update_section(current_content, link_text, MEMO_SECTION_HEADER)

            logging.info(f"Uploading updated daily note: {daily_note_path}")
            await asyncio.to_thread(
                self.dbx.files_upload,
                new_content.encode('utf-8'),
                daily_note_path,
                mode=WriteMode('overwrite')
            )
            logging.info(f"Link saved to Obsidian Daily Note ({MEMO_SECTION_HEADER}): {daily_note_path}")
            await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            await message.add_reaction(PROCESS_COMPLETE_EMOJI)

        except Exception as e:
            logging.error(f"[Save Title/Link Error] Failed to save title/link for {url}: {e}", exc_info=True)
            try:
                await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except discord.HTTPException: pass
            try:
                await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass


    async def trigger_clip_or_summary(self, message: discord.Message, url: str, is_youtube: bool): # Added is_youtube parameter
        """URLの種類に応じてWebクリップ処理を実行するか、YouTubeの場合はローカル処理を促す"""
        logging.info(f"Trigger function called for URL: {url} (Is YouTube: {is_youtube})") # Log determination result

        if is_youtube:
            # YouTube: Add trigger reaction for local worker
            logging.info(f"YouTube URL confirmed for {message.id}. Adding reaction '{YOUTUBE_REACTION_EMOJI}' for local worker.")
            try:
                logging.info(f"Adding reaction '{PROCESS_START_EMOJI}'...")
                await message.add_reaction(PROCESS_START_EMOJI)
                logging.info(f"Adding reaction '{YOUTUBE_REACTION_EMOJI}'...")
                await message.add_reaction(YOUTUBE_REACTION_EMOJI)
                logging.info(f"Removing reaction '{PROCESS_START_EMOJI}'...")
                await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                logging.info(f"Added '{YOUTUBE_REACTION_EMOJI}' reaction successfully.")
            except discord.HTTPException as e:
                logging.error(f"Failed to add/remove YouTube reactions to message {message.id}: {e}")
                try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                except discord.HTTPException: pass
                try: await message.add_reaction(PROCESS_ERROR_EMOJI)
                except discord.HTTPException: pass
            except Exception as e_reaction:
                 logging.error(f"Unexpected error during YouTube reaction handling for message {message.id}: {e_reaction}", exc_info=True)
                 try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                 except discord.HTTPException: pass
                 try: await message.add_reaction(PROCESS_ERROR_EMOJI)
                 except discord.HTTPException: pass

        else:
            # Web page: Perform web clip within this Cog
            if self.dbx:
                logging.info(f"Not a YouTube video URL. Calling internal _perform_web_clip for {url}")
                await self._perform_web_clip(url=url, message=message)
            else:
                logging.error("Cannot perform web clip: Dropbox client is not initialized.")
                await message.add_reaction(PROCESS_ERROR_EMOJI)


    async def _perform_web_clip(self, url: str, message: discord.Message):
        """Webクリップのコアロジック (旧 webclip_cog._perform_clip を async 化)"""
        if not self.dbx:
            logging.error("Cannot perform web clip: Dropbox client is not initialized.")
            await message.add_reaction(PROCESS_ERROR_EMOJI)
            return

        if any(r.emoji in (PROCESS_COMPLETE_EMOJI, PROCESS_ERROR_EMOJI) and r.me for r in message.reactions):
            logging.warning(f"Web clip for {url} already processed or failed. Skipping.")
            return

        await message.add_reaction(PROCESS_START_EMOJI)
        title = "Untitled"
        content_md = '(Content could not be extracted)'
        webclip_file_name = "" # Initialize for retry logic
        webclip_file_path = "" # Initialize for retry logic
        safe_title = "" # Initialize for retry logic


        try:
            logging.info(f"Starting web clip process for {url}")
            loop = asyncio.get_running_loop()
            title_result, content_md_result = await loop.run_in_executor(
                None, parse_url_with_readability, url
            )
            logging.info(f"Readability finished for {url}. Title: '{title_result}', Content length: {len(content_md_result) if content_md_result else 0}")

            title = title_result if title_result and title_result != "No Title Found" else url
            content_md = content_md_result or content_md

            safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
            if not safe_title:
                safe_title = "Untitled"
            safe_title = safe_title[:100]
            logging.debug(f"Sanitized title for filename: {safe_title}")

            now = datetime.now(JST)
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

            webclip_file_path = f"{self.dropbox_vault_path}/WebClips/{webclip_file_name}"

            # >>>>>>>>>>>>>>>>>> MODIFICATION #2 START <<<<<<<<<<<<<<<<<<
            # Upload WebClip file to Dropbox (with retry for conflicts)
            upload_successful = False
            for attempt in range(3):
                try:
                    logging.info(f"Uploading web clip file to Dropbox (Attempt {attempt+1}): {webclip_file_path}")
                    await asyncio.to_thread(
                        self.dbx.files_upload,
                        webclip_note_content.encode('utf-8'),
                        webclip_file_path,
                        mode=WriteMode('add') # Use 'add' to detect conflicts
                    )
                    logging.info(f"クリップ成功: {webclip_file_path}")
                    upload_successful = True
                    break # Exit loop on success
                except ApiError as e:
                    # Check if the error is a path conflict
                    if isinstance(e.error, dropbox.files.UploadError) and \
                       e.error.is_path() and \
                       e.error.get_path().is_conflict():
                        logging.warning(f"ファイル名競合: {webclip_file_name} → リトライ中 ({attempt+1}/3)")
                        # Generate a slightly different timestamp for the next attempt
                        await asyncio.sleep(0.5) # Small delay before retrying
                        timestamp = datetime.now(JST).strftime('%Y%m%d%H%M%S%f')[:-3] # Add milliseconds
                        webclip_file_name = f"{timestamp}-{safe_title}.md"
                        webclip_file_name_for_link = webclip_file_name.replace('.md', '') # Update link name too
                        webclip_file_path = f"{self.dropbox_vault_path}/WebClips/{webclip_file_name}"
                    else:
                        # Re-raise if it's not a conflict error we can handle
                        logging.error(f"Unhandled Dropbox API error during upload: {e}", exc_info=True)
                        raise e
                except Exception as upload_e:
                    # Catch other potential errors during upload
                    logging.error(f"Unexpected error during Dropbox upload (Attempt {attempt+1}): {upload_e}", exc_info=True)
                    # Optionally break or continue based on error type, here we raise it
                    raise upload_e

            if not upload_successful:
                logging.error(f"Failed to upload web clip file after 3 attempts due to conflicts or errors: {url}")
                await message.add_reaction(PROCESS_ERROR_EMOJI)
                # Ensure hourglass is removed before returning
                try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                except discord.HTTPException: pass
                return # Stop processing if upload failed

            # >>>>>>>>>>>>>>>>>> MODIFICATION #2 END <<<<<<<<<<<<<<<<<<


            # Update Daily Note (Only if upload was successful)
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"
            daily_note_content = ""
            try:
                logging.info(f"Downloading daily note for update: {daily_note_path}")
                _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                daily_note_content = res.content.decode('utf-8')
                logging.info(f"Daily note downloaded successfully.")
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    logging.info(f"デイリーノート {daily_note_path} は存在しないため、新規作成します。")
                    daily_note_content = f"# {daily_note_date}\n"
                else:
                    logging.error(f"Dropbox download error: {e}", exc_info=True)
                    raise

            link_to_add = f"- [[WebClips/{webclip_file_name_for_link}|{title}]]"
            logging.info(f"Adding link to daily note: {link_to_add}")

            new_daily_content = update_section(
                daily_note_content, link_to_add, WEBCLIPS_SECTION_HEADER
            )

            logging.info(f"Uploading updated daily note: {daily_note_path}")
            await asyncio.to_thread(
                self.dbx.files_upload,
                new_daily_content.encode('utf-8'),
                daily_note_path,
                mode=WriteMode('overwrite')
            )
            logging.info(f"デイリーノートを更新しました: {daily_note_path}")

            await message.add_reaction(PROCESS_COMPLETE_EMOJI)
            logging.info(f"Web clip process completed successfully for {url}")

        except Exception as e:
            logging.error(f"[Web Clip Error] Webクリップ処理中に予期せぬエラー ({url}): {e}", exc_info=True)
            try:
                await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass
        finally:
            try:
                await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except discord.HTTPException: pass


async def setup(bot: commands.Bot):
    """Cogセットアップ"""
    if MEMO_CHANNEL_ID == 0:
        logging.error("MemoCog: MEMO_CHANNEL_ID is not set or is 0. Cog will not be loaded.")
        return
    await bot.add_cog(MemoCog(bot))