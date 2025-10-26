import os
import discord
from discord.ext import commands
import asyncio
import logging
import dropbox
from dropbox.files import WriteMode, DownloadError # Added DownloadError
from dropbox.exceptions import ApiError # Added ApiError
from datetime import datetime, timezone, timedelta
import json
import re # Added re for URL detection
from obsidian_handler import add_memo_async
from utils.obsidian_utils import update_section # Import update_section
from web_parser import parse_url_with_readability # Import web parser
# Import youtube_cog function if possible, otherwise rely on bot.get_cog
# from cogs.youtube_cog import YOUTUBE_URL_REGEX, YouTubeCog # Example - adjust if needed

# --- Constants ---
try:
    import zoneinfo
    JST = zoneinfo.ZoneInfo("Asia/Tokyo")
except ImportError:
    JST = timezone(timedelta(hours=+9), "JST")

# Use channel ID from env var
MEMO_CHANNEL_ID = int(os.getenv("MEMO_CHANNEL_ID", 0))

# Reaction Emojis
TITLE_LINK_EMOJI = '🇹' # T for Title
CLIP_SUMMARY_EMOJI = '📄' # Page for Clip/Summary
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'

# URL Regex (borrowed from webclip_cog, consider putting in a central utils file)
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')
# YouTube Regex (borrowed from youtube_cog)
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})')

# >>>>>>>>>>>>>>>>>> MODIFICATION START <<<<<<<<<<<<<<<<<<
# Daily Note Section for Links (Use Memo section)
# LINKS_SECTION_HEADER = "## Links" # Removed
MEMO_SECTION_HEADER = "## Memo" # Use this for title/link saving as well
# >>>>>>>>>>>>>>>>>> MODIFICATION END <<<<<<<<<<<<<<<<<<


# Cog Class (Removed list functionality for clarity, focusing on URL/Memo)
class MemoCog(commands.Cog):
    """Discordの#memoチャンネルを監視し、テキストメモまたはURL処理を行うCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Store message IDs that are waiting for a reaction
        self.pending_url_messages = {} # {message_id: url}
        self.dbx = None # Initialize dbx client for saving links
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
                    app_secret=dbx_secret
                )
                self.dbx.users_get_current_account() # Test connection
                logging.info("MemoCog: Dropbox client initialized successfully.")
            except Exception as e:
                logging.error(f"MemoCog: Failed to initialize Dropbox client: {e}")
                self.dbx = None
        else:
            logging.warning("MemoCog: Dropbox credentials missing. Saving title/link to Obsidian will fail.")
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
                await asyncio.sleep(15)
                await message.remove_reaction("📝", self.bot.user)

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

        # Remove bot reactions first
        try:
            await message.clear_reactions() # Clear all reactions for simplicity
            # Or remove specific ones:
            # await message.remove_reaction(TITLE_LINK_EMOJI, self.bot.user)
            # await message.remove_reaction(CLIP_SUMMARY_EMOJI, self.bot.user)
        except discord.HTTPException:
            logging.warning(f"Failed to remove reactions from message {message.id}")

        # Process based on the reaction
        try:
            if emoji == TITLE_LINK_EMOJI:
                logging.info(f"Processing '{TITLE_LINK_EMOJI}' reaction for message {message.id} (URL: {url})")
                await self.save_title_and_link(message, url)

            elif emoji == CLIP_SUMMARY_EMOJI:
                logging.info(f"Processing '{CLIP_SUMMARY_EMOJI}' reaction for message {message.id} (URL: {url})")
                await self.trigger_clip_or_summary(message, url)

        except Exception as e:
             logging.error(f"Error processing reaction {emoji} for message {message.id}: {e}", exc_info=True)
             try:
                 await message.add_reaction(PROCESS_ERROR_EMOJI)
             except discord.HTTPException: pass


    async def save_title_and_link(self, message: discord.Message, url: str):
        """URLのタイトルを取得し、デイリーノートのMemoセクションにリンクとして保存する"""
        if not self.dbx:
            logging.error("Cannot save title/link: Dropbox client is not initialized.")
            await message.add_reaction(PROCESS_ERROR_EMOJI)
            return

        await message.add_reaction("⏳")
        title = "Untitled"
        is_youtube = False

        try:
            # Check if YouTube URL
            youtube_match = YOUTUBE_URL_REGEX.search(url)
            if youtube_match:
                is_youtube = True
                video_id = youtube_match.group(1)
                youtube_cog = self.bot.get_cog("YouTubeCog")
                if youtube_cog and hasattr(youtube_cog, 'get_video_info'):
                    video_info = await youtube_cog.get_video_info(video_id)
                    title = video_info.get('title', f"YouTube_{video_id}")
                else:
                    logging.warning("YouTubeCog or get_video_info not found, cannot get video title.")
                    title = f"YouTube_{video_id}" # Fallback title
            else:
                # General Web URL - Use web_parser
                fetched_title, _ = await asyncio.to_thread(parse_url_with_readability, url)
                if fetched_title and fetched_title != "No Title Found":
                    title = fetched_title
                else:
                    # Fallback if readability fails
                    title = url.split('/')[-1] or url # Use last part of URL or full URL

            # Format the link for Obsidian Daily Note
            now = datetime.now(JST)
            date_str = now.strftime('%Y-%m-%d')
            time_str = now.strftime('%H:%M') # Add timestamp

            # >>>>>>>>>>>>>>>>>> MODIFICATION START <<<<<<<<<<<<<<<<<<
            # Format similar to regular memo, but with link
            # Obsidian link format: - [[URL|Title]] or - [Title](URL)
            # Use standard markdown link within the memo format
            link_text = f"- {time_str} [{title}]({url})"
            # >>>>>>>>>>>>>>>>>> MODIFICATION END <<<<<<<<<<<<<<<<<<

            # Get Daily Note path
            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
            current_content = ""

            # Download or create Daily Note content
            try:
                _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                current_content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    current_content = f"# {date_str}\n" # Create new
                    logging.info(f"Creating new daily note: {daily_note_path}")
                else:
                    raise # Re-raise other API errors

            # >>>>>>>>>>>>>>>>>> MODIFICATION START <<<<<<<<<<<<<<<<<<
            # Update the ## Memo section
            new_content = update_section(current_content, link_text, MEMO_SECTION_HEADER)
            # >>>>>>>>>>>>>>>>>> MODIFICATION END <<<<<<<<<<<<<<<<<<

            # Upload the updated note
            await asyncio.to_thread(
                self.dbx.files_upload,
                new_content.encode('utf-8'),
                daily_note_path,
                mode=WriteMode('overwrite')
            )
            logging.info(f"Link saved to Obsidian Daily Note ({MEMO_SECTION_HEADER}): {daily_note_path}") # Log updated section
            await message.remove_reaction("⏳", self.bot.user)
            await message.add_reaction(PROCESS_COMPLETE_EMOJI)

        except Exception as e:
            logging.error(f"Failed to save title/link for {url}: {e}", exc_info=True)
            try:
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass


    async def trigger_clip_or_summary(self, message: discord.Message, url: str):
        """URLの種類に応じてWebClipCogまたはYouTubeCogの処理を呼び出す"""
        is_youtube = YOUTUBE_URL_REGEX.search(url)

        if is_youtube:
            youtube_cog = self.bot.get_cog("YouTubeCog")
            if youtube_cog and hasattr(youtube_cog, 'perform_summary_async'):
                logging.info(f"Calling YouTubeCog.perform_summary_async for {url}")
                # Pass the original message object so YouTubeCog can manage reactions
                await youtube_cog.perform_summary_async(url=url, message=message)
            else:
                logging.error("YouTubeCog or perform_summary_async method not found.")
                await message.add_reaction(PROCESS_ERROR_EMOJI)
        else:
            webclip_cog = self.bot.get_cog("WebClipCog")
            if webclip_cog and hasattr(webclip_cog, 'perform_clip_async'):
                logging.info(f"Calling WebClipCog.perform_clip_async for {url}")
                 # Pass the original message object so WebClipCog can manage reactions
                await webclip_cog.perform_clip_async(url=url, message=message)
            else:
                logging.error("WebClipCog or perform_clip_async method not found.")
                await message.add_reaction(PROCESS_ERROR_EMOJI)

    # Remove previous list functionality if not needed anymore
    # async def add_item_to_list_file(...) -> bool: ...


async def setup(bot: commands.Bot):
    """Cogセットアップ"""
    # Check if MEMO_CHANNEL_ID is set, otherwise don't load
    if MEMO_CHANNEL_ID == 0:
        logging.error("MemoCog: MEMO_CHANNEL_ID is not set or is 0. Cog will not be loaded.")
        return
    await bot.add_cog(MemoCog(bot))