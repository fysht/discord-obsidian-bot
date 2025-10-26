import os
import discord
from discord.ext import commands
import asyncio
import logging
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
from datetime import datetime, timezone, timedelta
import json
import re
from obsidian_handler import add_memo_async
from utils.obsidian_utils import update_section
from web_parser import parse_url_with_readability # web_parserをインポート

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
YOUTUBE_REACTION_EMOJI = '▶️' # YouTube URL用に一時的につけるリアクション

# URL Regex (borrowed from webclip_cog, consider putting in a central utils file)
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')
# YouTube Regex (borrowed from youtube_cog)
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
        except discord.HTTPException:
            logging.warning(f"Failed to remove reactions from message {message.id}")

        # Process based on the reaction
        try:
            if emoji == TITLE_LINK_EMOJI:
                logging.info(f"Processing '{TITLE_LINK_EMOJI}' reaction for message {message.id} (URL: {url})")
                await self.save_title_and_link(message, url)

            elif emoji == CLIP_SUMMARY_EMOJI:
                logging.info(f"Processing '{CLIP_SUMMARY_EMOJI}' reaction for message {message.id} (URL: {url})")
                await self.trigger_clip_or_summary(message, url) # 名前はそのまま流用

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
                # YouTubeCogはローカル実行のため、ここでは直接タイトルを取得
                try:
                    # youtube_cog.py の get_video_info 相当の処理をここで行う (aiohttpを使用)
                    async with aiohttp.ClientSession() as session:
                         oembed_url = f"https://www.youtube.com/oembed?url=http://www.youtube.com/watch?v={video_id}&format=json"
                         async with session.get(oembed_url) as response:
                             if response.status == 200:
                                 data = await response.json()
                                 title = data.get("title", f"YouTube_{video_id}")
                             else:
                                 logging.warning(f"oEmbed failed for {video_id}: Status {response.status}")
                                 title = f"YouTube_{video_id}"
                except Exception as e_yt_title:
                     logging.error(f"Error fetching YouTube title via oEmbed: {e_yt_title}")
                     title = f"YouTube_{video_id}"
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

            # Format similar to regular memo, but with link
            link_text = f"- {time_str} [{title}]({url})"

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

            # Update the ## Memo section
            new_content = update_section(current_content, link_text, MEMO_SECTION_HEADER)

            # Upload the updated note
            await asyncio.to_thread(
                self.dbx.files_upload,
                new_content.encode('utf-8'),
                daily_note_path,
                mode=WriteMode('overwrite')
            )
            logging.info(f"Link saved to Obsidian Daily Note ({MEMO_SECTION_HEADER}): {daily_note_path}")
            await message.remove_reaction("⏳", self.bot.user)
            await message.add_reaction(PROCESS_COMPLETE_EMOJI)

        except Exception as e:
            logging.error(f"Failed to save title/link for {url}: {e}", exc_info=True)
            try:
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass


    async def trigger_clip_or_summary(self, message: discord.Message, url: str):
        """URLの種類に応じてWebクリップ処理を実行するか、YouTubeの場合はローカル処理を促す"""
        is_youtube = YOUTUBE_URL_REGEX.search(url)

        if is_youtube:
            # YouTubeの場合は、ローカルワーカーが処理することを伝えるリアクションを付ける
            logging.info(f"YouTube URL detected for {message.id}. Adding reaction '{YOUTUBE_REACTION_EMOJI}' for local worker.")
            try:
                # ユーザーがどの処理を選んだか分かりやすくするため一時的にリアクションを付ける
                await message.add_reaction(YOUTUBE_REACTION_EMOJI)
                # ここでは local_worker.py が `on_raw_reaction_add` を見て処理することを期待する
                # 必要であれば、local_worker.py 側で処理完了後にこのリアクションを消す
            except discord.HTTPException as e:
                logging.error(f"Failed to add YouTube reaction to message {message.id}: {e}")
                await message.add_reaction(PROCESS_ERROR_EMOJI) # リアクション付与失敗時

        else:
            # Webページの場合は、このCog内でクリップ処理を実行
            if self.dbx:
                logging.info(f"Calling internal _perform_web_clip for {url}")
                await self._perform_web_clip(url=url, message=message) # 内部メソッドを呼び出す
            else:
                logging.error("Cannot perform web clip: Dropbox client is not initialized.")
                await message.add_reaction(PROCESS_ERROR_EMOJI)

    # >>>>>>>>>>>>>>>>>> MODIFICATION START <<<<<<<<<<<<<<<<<<
    # Integrate _perform_clip logic from webclip_cog.py here
    async def _perform_web_clip(self, url: str, message: discord.Message):
        """Webクリップのコアロジック (旧 webclip_cog._perform_clip)"""
        if not self.dbx: # Check again, although likely checked before calling
            logging.error("Cannot perform web clip: Dropbox client is not initialized.")
            await message.add_reaction(PROCESS_ERROR_EMOJI)
            return

        try:
            await message.add_reaction("⏳")

            loop = asyncio.get_running_loop()
            title, content_md = await loop.run_in_executor(
                None, parse_url_with_readability, url
            )

            # Use original title if available, otherwise fallback
            original_title = title if title and title != "No Title Found" else url.split('/')[-1] or "Untitled"

            safe_title = re.sub(r'[\\/*?:"<>|]', "_", original_title) # Replace invalid chars with underscore
            if not safe_title:
                safe_title = "Untitled"
            safe_title = safe_title[:100] # Limit filename length

            now = datetime.now(JST)
            timestamp = now.strftime('%Y%m%d%H%M%S')
            daily_note_date = now.strftime('%Y-%m-%d')

            webclip_file_name = f"{timestamp}-{safe_title}.md"
            webclip_file_name_for_link = webclip_file_name.replace('.md', '')

            # Use original title for H1, ensure content_md is not None
            webclip_note_content = (
                f"# {original_title}\n\n"
                f"- **Source:** <{url}>\n"
                f"- **Clipped:** {now.strftime('%Y-%m-%d %H:%M')}\n\n" # Added clipped time
                f"---\n\n"
                f"[[{daily_note_date}]]\n\n"
                f"{content_md or '(Content could not be extracted)'}" # Handle potential None content
            )

            # Use initialized dbx client directly (no 'with' needed if initialized in __init__)
            webclip_file_path = f"{self.dropbox_vault_path}/WebClips/{webclip_file_name}"

            # Run Dropbox upload in executor thread
            await asyncio.to_thread(
                self.dbx.files_upload,
                webclip_note_content.encode('utf-8'),
                webclip_file_path,
                mode=WriteMode('add')
            )
            logging.info(f"クリップ成功: {webclip_file_path}")

            daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"
            daily_note_content = ""
            try:
                # Run Dropbox download in executor thread
                _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                daily_note_content = res.content.decode('utf-8')
            except ApiError as e:
                if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                    logging.info(f"デイリーノート {daily_note_path} は存在しないため、新規作成します。")
                    daily_note_content = f"# {daily_note_date}\n" # Create new content
                else:
                    raise # Re-raise other API errors

            # Use original_title in the link as well
            link_to_add = f"- [[WebClips/{webclip_file_name_for_link}|{original_title}]]"

            new_daily_content = update_section(
                daily_note_content, link_to_add, WEBCLIPS_SECTION_HEADER
            )

            # Run Dropbox upload in executor thread
            await asyncio.to_thread(
                self.dbx.files_upload,
                new_daily_content.encode('utf-8'),
                daily_note_path,
                mode=WriteMode('overwrite')
            )
            logging.info(f"デイリーノートを更新しました: {daily_note_path}")

            await message.add_reaction(PROCESS_COMPLETE_EMOJI)

        except Exception as e:
            logging.error(f"Webクリップ処理中にエラー: {e}", exc_info=True)
            try:
                await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass # Ignore if reaction fails
        finally:
            # Ensure the hourglass is removed even if errors occur
            try:
                await message.remove_reaction("⏳", self.bot.user)
            except discord.HTTPException: pass
    # >>>>>>>>>>>>>>>>>> MODIFICATION END <<<<<<<<<<<<<<<<<<


async def setup(bot: commands.Bot):
    """Cogセットアップ"""
    # Check if MEMO_CHANNEL_ID is set, otherwise don't load
    if MEMO_CHANNEL_ID == 0:
        logging.error("MemoCog: MEMO_CHANNEL_ID is not set or is 0. Cog will not be loaded.")
        return
    await bot.add_cog(MemoCog(bot))