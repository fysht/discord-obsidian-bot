import os
import discord
from discord.ext import commands
import asyncio
import logging
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
from datetime import datetime, timezone, timedelta # timedelta をインポート
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
        self.pending_url_messages = {} # {message_id: url}
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
            # Found a URL, add reactions and store for later
            url = url_match.group(0)
            logging.info(f"URL detected in message {message.id}: {url}")
            self.pending_url_messages[message.id] = url
            try:
                await message.add_reaction(TITLE_LINK_EMOJI)
                await message.add_reaction(CLIP_SUMMARY_EMOJI)
                # Optional: Add a timeout task to remove reactions/entry later
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
                # Add a temporary confirmation reaction to the original message
                await message.add_reaction("📝")
                # Optionally, delete the reaction after some time
                # Use create_task to avoid blocking on_message
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
        # Ignore bot reactions and reactions in other channels
        if payload.user_id == self.bot.user.id or payload.channel_id != MEMO_CHANNEL_ID:
            return

        # Check if the reaction is on a message we are tracking
        if payload.message_id not in self.pending_url_messages:
            return

        # Check if the emoji is one of the triggers
        emoji = str(payload.emoji)
        if emoji not in [TITLE_LINK_EMOJI, CLIP_SUMMARY_EMOJI]:
            return

        # Get the original message and URL
        url = self.pending_url_messages.pop(payload.message_id, None) # Remove from pending
        if not url:
            logging.warning(f"Reaction {emoji} on message {payload.message_id} but URL not found in pending list.")
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return
        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            logging.error(f"Failed to fetch message {payload.message_id} for reaction processing.")
            return

        # --- Check if already processing ---
        is_processing = False
        for reaction in message.reactions:
            if reaction.emoji == PROCESS_START_EMOJI and reaction.me:
                is_processing = True
                break
        if is_processing:
            logging.warning(f"Message {message.id} is already being processed. Ignoring reaction {emoji}.")
            return
        # --- End check ---


        # Remove bot choice reactions first
        try:
            # Use await message.clear_reactions() if preferred, but removing specific ones is safer
            await message.remove_reaction(TITLE_LINK_EMOJI, self.bot.user)
            await message.remove_reaction(CLIP_SUMMARY_EMOJI, self.bot.user)
        except discord.HTTPException:
            logging.warning(f"Failed to remove initial reactions from message {message.id}")

        # Process based on the reaction
        try:
            if emoji == TITLE_LINK_EMOJI:
                logging.info(f"Processing '{TITLE_LINK_EMOJI}' reaction for message {message.id} (URL: {url})")
                await self.save_title_and_link(message, url)

            elif emoji == CLIP_SUMMARY_EMOJI:
                logging.info(f"Processing '{CLIP_SUMMARY_EMOJI}' reaction for message {message.id} (URL: {url})")
                await self.trigger_clip_or_summary(message, url)

        except Exception as e:
             logging.error(f"[Reaction Processing Error] Error processing reaction {emoji} for message {message.id}: {e}", exc_info=True) # Added prefix
             try:
                 # Ensure hourglass is removed on error before adding error emoji
                 await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
             except discord.HTTPException: pass
             try:
                 await message.add_reaction(PROCESS_ERROR_EMOJI)
             except discord.HTTPException: pass


    async def save_title_and_link(self, message: discord.Message, url: str):
        """URLのタイトルを取得し、デイリーノートのMemoセクションにリンクとして保存する"""
        if not self.dbx:
            logging.error("Cannot save title/link: Dropbox client is not initialized.")
            await message.add_reaction(PROCESS_ERROR_EMOJI)
            return

        await message.add_reaction(PROCESS_START_EMOJI) # Use defined constant
        title = "Untitled"
        is_youtube = False

        try:
            # Check if YouTube URL
            youtube_match = YOUTUBE_URL_REGEX.search(url)
            if youtube_match:
                is_youtube = True
                video_id = youtube_match.group(1)
                logging.info(f"Fetching title for YouTube video ID: {video_id}") # Add log
                try:
                    # Fetch title using aiohttp
                    async with aiohttp.ClientSession() as session:
                         oembed_url = f"https://www.youtube.com/oembed?url=http://www.youtube.com/watch?v={video_id}&format=json"
                         async with session.get(oembed_url, timeout=10) as response: # Add timeout
                             if response.status == 200:
                                 data = await response.json()
                                 title = data.get("title", f"YouTube_{video_id}")
                                 logging.info(f"YouTube title fetched: {title}") # Add log
                             else:
                                 logging.warning(f"oEmbed failed for {video_id}: Status {response.status}")
                                 title = f"YouTube_{video_id}"
                except asyncio.TimeoutError:
                    logging.error("Timeout fetching YouTube title via oEmbed.")
                    title = f"YouTube_{video_id}"
                except Exception as e_yt_title:
                     logging.error(f"Error fetching YouTube title via oEmbed: {e_yt_title}", exc_info=True) # Log traceback
                     title = f"YouTube_{video_id}"
            else:
                # General Web URL - Use web_parser
                logging.info(f"Fetching title for web URL: {url}") # Add log
                # Run potentially blocking parse_url_with_readability in an executor
                loop = asyncio.get_running_loop()
                fetched_title, _ = await loop.run_in_executor(
                    None, parse_url_with_readability, url
                )
                if fetched_title and fetched_title != "No Title Found":
                    title = fetched_title
                    logging.info(f"Web title fetched: {title}") # Add log
                else:
                    # Fallback if readability fails or returns "No Title Found"
                    logging.warning(f"Failed to fetch title using readability for {url}. Using URL as title.") # Add log
                    title = url # Use full URL as fallback title for web links

            # Format the link for Obsidian Daily Note
            now = datetime.now(JST)
            date_str = now.strftime('%Y-%m-%d')
            time_str = now.strftime('%H:%M') # Add timestamp

            # Format similar to regular memo, but with link
            link_text = f"- {time_str} [{title}]({url})"
            logging.debug(f"Formatted link text: {link_text}") # Add log

            # Get Daily Note path
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
            current_content = ""

            # Download or create Daily Note content (using to_thread)
            try:
                logging.info(f"Downloading daily note: {daily_note_path}") # Add log
                # Use asyncio.to_thread for potentially blocking Dropbox calls
                _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                current_content = res.content.decode('utf-8')
                logging.info(f"Daily note downloaded successfully.") # Add log
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    current_content = f"# {date_str}\n" # Create new
                    logging.info(f"Creating new daily note: {daily_note_path}")
                else:
                    logging.error(f"Dropbox download error: {e}", exc_info=True) # Log traceback
                    raise # Re-raise other API errors

            # Update the ## Memo section
            logging.info(f"Updating section '{MEMO_SECTION_HEADER}' in daily note.") # Add log
            new_content = update_section(current_content, link_text, MEMO_SECTION_HEADER)

            # Upload the updated note (using to_thread)
            logging.info(f"Uploading updated daily note: {daily_note_path}") # Add log
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
            logging.error(f"[Save Title/Link Error] Failed to save title/link for {url}: {e}", exc_info=True) # Log traceback and add prefix
            try:
                await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except discord.HTTPException: pass
            try:
                await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass


    # >>>>>>>>>>>>>>>>>> MODIFICATION START (Added logging and refined reaction logic) <<<<<<<<<<<<<<<<<<
    async def trigger_clip_or_summary(self, message: discord.Message, url: str):
        """URLの種類に応じてWebクリップ処理を実行するか、YouTubeの場合はローカル処理を促す"""
        logging.info(f"Trigger function called for URL: {url}") # Log function call
        is_youtube = False # Initialize
        youtube_match = None # Initialize
        try:
            logging.info(f"Attempting regex search on URL: {url}")
            youtube_match = YOUTUBE_URL_REGEX.search(url)

            if youtube_match:
                logging.info(f"Regex matched! Video ID found: {youtube_match.group(1)}") # Log match success and ID
                is_youtube = True
            else:
                logging.warning(f"Regex did NOT match for URL: {url}") # Log match failure
            logging.info(f"Is YouTube URL determination complete. Result: {is_youtube}") # Log determination result

        except Exception as e_regex:
            logging.error(f"Error during regex search for URL '{url}': {e_regex}", exc_info=True)
            # Treat regex error as non-YouTube, but add error reaction
            is_youtube = False
            try:
                 await message.add_reaction(PROCESS_ERROR_EMOJI) # Indicate regex error
            except discord.HTTPException: pass
            return # Stop processing

        if is_youtube:
            # YouTube: Add trigger reaction for local worker
            logging.info(f"YouTube URL confirmed for {message.id}. Adding reaction '{YOUTUBE_REACTION_EMOJI}' for local worker.")
            try:
                # Add reactions sequentially and log each step
                logging.info(f"Adding reaction '{PROCESS_START_EMOJI}'...")
                await message.add_reaction(PROCESS_START_EMOJI)
                logging.info(f"Adding reaction '{YOUTUBE_REACTION_EMOJI}'...")
                await message.add_reaction(YOUTUBE_REACTION_EMOJI)
                logging.info(f"Removing reaction '{PROCESS_START_EMOJI}'...")
                await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                logging.info(f"Added '{YOUTUBE_REACTION_EMOJI}' reaction successfully.")
            except discord.HTTPException as e:
                logging.error(f"Failed to add/remove YouTube reactions to message {message.id}: {e}")
                # Clean up potentially added hourglass on error
                try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                except discord.HTTPException: pass
                # Add error emoji if reaction management failed
                try: await message.add_reaction(PROCESS_ERROR_EMOJI)
                except discord.HTTPException: pass
            except Exception as e_reaction: # Catch other potential errors during reaction handling
                 logging.error(f"Unexpected error during YouTube reaction handling for message {message.id}: {e_reaction}", exc_info=True)
                 try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
                 except discord.HTTPException: pass
                 try: await message.add_reaction(PROCESS_ERROR_EMOJI)
                 except discord.HTTPException: pass

        else:
            # Web page: Perform web clip within this Cog
            if self.dbx:
                logging.info(f"Not a YouTube video URL. Calling internal _perform_web_clip for {url}")
                await self._perform_web_clip(url=url, message=message) # Call internal method
            else:
                logging.error("Cannot perform web clip: Dropbox client is not initialized.")
                await message.add_reaction(PROCESS_ERROR_EMOJI)
    # >>>>>>>>>>>>>>>>>> MODIFICATION END <<<<<<<<<<<<<<<<<<


    # Use logic based on the user-provided webclip_cog.py's _perform_clip
    async def _perform_web_clip(self, url: str, message: discord.Message):
        """Webクリップのコアロジック (旧 webclip_cog._perform_clip を async 化)"""
        if not self.dbx:
            logging.error("Cannot perform web clip: Dropbox client is not initialized.")
            await message.add_reaction(PROCESS_ERROR_EMOJI)
            return

        # Check if already processed
        if any(r.emoji in (PROCESS_COMPLETE_EMOJI, PROCESS_ERROR_EMOJI) and r.me for r in message.reactions):
            logging.warning(f"Web clip for {url} already processed or failed. Skipping.")
            return

        await message.add_reaction(PROCESS_START_EMOJI)
        title = "Untitled"
        content_md = '(Content could not be extracted)'

        try:
            logging.info(f"Starting web clip process for {url}") # Add log
            loop = asyncio.get_running_loop()
            # Run blocking parsing in executor
            title_result, content_md_result = await loop.run_in_executor(
                None, parse_url_with_readability, url
            )
            logging.info(f"Readability finished for {url}. Title: '{title_result}', Content length: {len(content_md_result) if content_md_result else 0}") # Add log

            # Use original title if available, otherwise fallback
            title = title_result if title_result and title_result != "No Title Found" else url

            # Use parsed content if available
            content_md = content_md_result or content_md

            # Sanitize title for filename (using original code's method: remove invalid chars)
            safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
            if not safe_title:
                safe_title = "Untitled"
            # Limit filename length (optional, add if needed)
            safe_title = safe_title[:100] # Limit to 100 chars
            logging.debug(f"Sanitized title for filename: {safe_title}") # Add log

            now = datetime.now(JST)
            timestamp = now.strftime('%Y%m%d%H%M%S')
            daily_note_date = now.strftime('%Y-%m-%d')

            webclip_file_name = f"{timestamp}-{safe_title}.md"
            webclip_file_name_for_link = webclip_file_name.replace('.md', '')

            # Create note content (using original code's format)
            webclip_note_content = (
                f"# {title}\n\n"
                f"- **Source:** <{url}>\n\n" # Removed Clipped time from original snippet
                f"---\n\n"
                f"[[{daily_note_date}]]\n\n"
                f"{content_md}"
            )

            # Upload WebClip file to Dropbox (using to_thread)
            webclip_file_path = f"{self.dropbox_vault_path}/WebClips/{webclip_file_name}"
            logging.info(f"Uploading web clip file to Dropbox: {webclip_file_path}") # Add log
            await asyncio.to_thread(
                self.dbx.files_upload,
                webclip_note_content.encode('utf-8'),
                webclip_file_path,
                mode=WriteMode('add')
            )
            logging.info(f"クリップ成功: {webclip_file_path}")

            # Update Daily Note
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"
            daily_note_content = ""
            try:
                # Download daily note (using to_thread)
                logging.info(f"Downloading daily note for update: {daily_note_path}") # Add log
                _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                daily_note_content = res.content.decode('utf-8')
                logging.info(f"Daily note downloaded successfully.") # Add log
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    logging.info(f"デイリーノート {daily_note_path} は存在しないため、新規作成します。")
                    # Create basic content if note doesn't exist
                    daily_note_content = f"# {daily_note_date}\n"
                else:
                    logging.error(f"Dropbox download error: {e}", exc_info=True) # Log traceback
                    raise # Re-raise other API errors

            # Create link using the format from original webclip_cog.py
            link_to_add = f"- [[WebClips/{webclip_file_name_for_link}|{title}]]" # Use title in link display
            logging.info(f"Adding link to daily note: {link_to_add}") # Add log

            new_daily_content = update_section(
                daily_note_content, link_to_add, WEBCLIPS_SECTION_HEADER
            )

            # Upload updated daily note (using to_thread)
            logging.info(f"Uploading updated daily note: {daily_note_path}") # Add log
            await asyncio.to_thread(
                self.dbx.files_upload,
                new_daily_content.encode('utf-8'),
                daily_note_path,
                mode=WriteMode('overwrite')
            )
            logging.info(f"デイリーノートを更新しました: {daily_note_path}")

            # Add success reaction
            await message.add_reaction(PROCESS_COMPLETE_EMOJI)
            logging.info(f"Web clip process completed successfully for {url}") # Add log

        except Exception as e:
            logging.error(f"[Web Clip Error] Webクリップ処理中にエラー ({url}): {e}", exc_info=True) # Log traceback and add prefix
            try:
                # Add error reaction
                await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass # Ignore if reaction fails
        finally:
            # Ensure the hourglass is removed even if errors occur
            try:
                await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except discord.HTTPException: pass


async def setup(bot: commands.Bot):
    """Cogセットアップ"""
    if MEMO_CHANNEL_ID == 0:
        logging.error("MemoCog: MEMO_CHANNEL_ID is not set or is 0. Cog will not be loaded.")
        return
    await bot.add_cog(MemoCog(bot))