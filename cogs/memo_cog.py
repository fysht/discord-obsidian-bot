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

# Reaction Emojis (Triggered by User)
TITLE_LINK_EMOJI = '🇹'
CLIP_EMOJI = '📄'
SUMMARY_EMOJI = '🎬'
# Bot Status Emojis
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'
PROCESS_START_EMOJI = '⏳'
YOUTUBE_WORKER_TRIGGER_EMOJI = '▶️' # Added by bot for local worker

# URL Regex
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')
# YouTube Regex (ビデオIDを抽出できる形式のみ - Title fetching でのみ使用)
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})')

# Daily Note Section Header
MEMO_SECTION_HEADER = "## Memo"
WEBCLIPS_SECTION_HEADER = "## WebClips" # Webクリップ保存用

# Cog Class
class MemoCog(commands.Cog):
    """Discordの#memoチャンネルを監視し、テキストメモ保存、またはユーザーリアクションに応じてURL処理を行うCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.dbx = None # Initialize dbx client
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
                    timeout=60
                )
                self.dbx.users_get_current_account()
                logging.info("MemoCog: Dropbox client initialized successfully.")
            except Exception as e:
                logging.error(f"MemoCog: Failed to initialize Dropbox client: {e}")
                self.dbx = None
        else:
            logging.warning("MemoCog: Dropbox credentials missing. Saving title/link/clips to Obsidian will fail.")
            self.dbx = None
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")


    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """#memo チャンネルに投稿されたメッセージを処理 (URLの場合は何もしない、テキストのみ保存)"""
        if message.author.bot or message.channel.id != MEMO_CHANNEL_ID:
            return

        content = message.content.strip()
        if not content:
            return

        # Check for URL
        url_match = URL_REGEX.search(content)
        if url_match:
            # Found a URL. Bot does nothing, waits for user reaction.
            logging.info(f"URL detected in message {message.id}. Waiting for user reaction (🇹, 📄, or 🎬).")
            # Optionally add a neutral reaction like '👀' to indicate the bot saw the URL
            # try:
            #     await message.add_reaction("👀")
            # except discord.HTTPException:
            #     pass
        else:
            # Not a URL, treat as a regular memo
            logging.info(f"Text memo detected in message {message.id}. Saving via obsidian_handler.")
            try:
                await add_memo_async(
                    content=content,
                    author=str(message.author),
                    created_at=message.created_at.isoformat(),
                    message_id=message.id,
                    context="General",
                    category="Memo"
                )
                await message.add_reaction("📝")
                async def remove_temp_reaction(msg, emoji):
                    await asyncio.sleep(15)
                    try: await msg.remove_reaction(emoji, self.bot.user)
                    except discord.HTTPException: pass
                asyncio.create_task(remove_temp_reaction(message, "📝"))

            except Exception as e:
                logging.error(f"Failed to save text memo using add_memo_async: {e}", exc_info=True)
                await message.add_reaction(PROCESS_ERROR_EMOJI)


    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """ユーザーが付けたリアクションに応じてURLメッセージを処理"""
        # Ignore bot reactions and reactions in other channels
        if payload.user_id == self.bot.user.id or payload.channel_id != MEMO_CHANNEL_ID:
            return

        emoji = str(payload.emoji)
        # Check if the emoji is one of the triggers added by the USER
        if emoji not in [TITLE_LINK_EMOJI, CLIP_EMOJI, SUMMARY_EMOJI]:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return
        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            logging.error(f"Failed to fetch message {payload.message_id} for reaction processing.")
            return

        # --- Check if the message content actually contains a URL ---
        content = message.content.strip()
        url_match = URL_REGEX.search(content)
        if not url_match:
            logging.warning(f"User reaction {emoji} added to message {message.id} which does not contain a URL.")
            # Optionally remove the user reaction
            try:
                user = await self.bot.fetch_user(payload.user_id)
                if user: await message.remove_reaction(payload.emoji, user)
            except discord.HTTPException: pass
            return
        url = url_match.group(0)
        # --- End URL check ---

        # --- Check if already processing by bot ---
        if any(r.emoji == PROCESS_START_EMOJI and r.me for r in message.reactions):
            logging.warning(f"Message {message.id} is already being processed. Ignoring user reaction {emoji}.")
            # Optionally remove the user reaction
            try:
                user = await self.bot.fetch_user(payload.user_id)
                if user: await message.remove_reaction(payload.emoji, user)
            except discord.HTTPException: pass
            return
        # --- End check ---

        # Remove the user's trigger reaction
        try:
            user = await self.bot.fetch_user(payload.user_id)
            if user:
                await message.remove_reaction(payload.emoji, user)
                logging.info(f"Removed user reaction {emoji} from message {message.id}")
        except discord.HTTPException:
            logging.warning(f"Failed to remove user reaction {emoji} from message {message.id}")

        # >>>>>>>>>>>>>>>>>> MODIFICATION START <<<<<<<<<<<<<<<<<<
        # --- Process based ONLY on the user's reaction ---
        logging.info(f"Processing user reaction {emoji} for URL: {url}")
        try:
            if emoji == TITLE_LINK_EMOJI:
                logging.info(f"Action: Save Title and Link")
                # Need to determine type inside save_title_and_link for title fetching
                await self.save_title_and_link(message, url) # Removed is_youtube flag

            elif emoji == CLIP_EMOJI:
                logging.info(f"Action: Perform Web Clip (regardless of URL type)")
                # Directly call web clip function
                await self._perform_web_clip(message, url)

            elif emoji == SUMMARY_EMOJI:
                logging.info(f"Action: Signal YouTube Worker (regardless of URL type)")
                # Directly signal the worker; worker should handle invalid URLs
                await self.signal_youtube_worker(message)

        except Exception as e:
             logging.error(f"[Reaction Processing Error] Error processing user reaction {emoji} for message {message.id}: {e}", exc_info=True)
             try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
             except discord.HTTPException: pass
             try: await message.add_reaction(PROCESS_ERROR_EMOJI)
             except discord.HTTPException: pass
        # >>>>>>>>>>>>>>>>>> MODIFICATION END <<<<<<<<<<<<<<<<<<


    async def save_title_and_link(self, message: discord.Message, url: str): # Removed is_youtube parameter
        """URLのタイトルを取得し、デイリーノートのMemoセクションにリンクとして保存する"""
        if not self.dbx:
            logging.error("Cannot save title/link: Dropbox client is not initialized.")
            await message.add_reaction(PROCESS_ERROR_EMOJI)
            return

        await message.add_reaction(PROCESS_START_EMOJI)
        title = "Untitled"

        try:
            # --- Determine URL type HERE for title fetching ---
            youtube_match = YOUTUBE_URL_REGEX.search(url)
            is_youtube = bool(youtube_match)
            # --- End determination ---

            if is_youtube:
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
            try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except discord.HTTPException: pass
            try: await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass


    async def signal_youtube_worker(self, message: discord.Message):
        """Adds the trigger emoji for the local YouTube worker."""
        logging.info(f"Signaling local worker for YouTube summary: Message {message.id}")
        try:
            await message.add_reaction(PROCESS_START_EMOJI)
            await message.add_reaction(YOUTUBE_WORKER_TRIGGER_EMOJI)
            await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            logging.info(f"Added '{YOUTUBE_WORKER_TRIGGER_EMOJI}' reaction successfully.")
        except discord.HTTPException as e:
            logging.error(f"Failed to add/remove YouTube worker trigger reactions to message {message.id}: {e}")
            try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except discord.HTTPException: pass
            try: await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass
        except Exception as e_reaction:
             logging.error(f"Unexpected error during YouTube worker signaling for message {message.id}: {e_reaction}", exc_info=True)
             try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
             except discord.HTTPException: pass
             try: await message.add_reaction(PROCESS_ERROR_EMOJI)
             except discord.HTTPException: pass


    async def _perform_web_clip(self, message: discord.Message, url: str):
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
        webclip_file_name = ""
        webclip_file_path = ""
        safe_title = ""


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
                    if isinstance(e.error, dropbox.files.UploadError) and \
                       e.error.is_path() and \
                       e.error.get_path().is_conflict():
                        logging.warning(f"ファイル名競合: {webclip_file_name} → リトライ中 ({attempt+1}/3)")
                        await asyncio.sleep(0.5)
                        timestamp = datetime.now(JST).strftime('%Y%m%d%H%M%S%f')[:-3]
                        webclip_file_name = f"{timestamp}-{safe_title}.md"
                        webclip_file_name_for_link = webclip_file_name.replace('.md', '')
                        webclip_file_path = f"{self.dropbox_vault_path}/WebClips/{webclip_file_name}"
                    else:
                        logging.error(f"Unhandled Dropbox API error during upload: {e}", exc_info=True)
                        raise e
                except Exception as upload_e:
                    logging.error(f"Unexpected error during Dropbox upload (Attempt {attempt+1}): {upload_e}", exc_info=True)
                    raise upload_e

            if not upload_successful:
                logging.error(f"Failed to upload web clip file after 3 attempts due to conflicts or errors: {url}")
                raise Exception("Failed to upload web clip file after retries.")


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