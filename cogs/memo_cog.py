import discord
from discord.ext import commands
import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
import zoneinfo
from dropbox.files import WriteMode
from dropbox.exceptions import ApiError

from obsidian_handler import append_text_to_doc_async, update_section
from webclip_handler import parse_url_with_readability
from youtube_handler import append_youtube_summary_async

JST = zoneinfo.ZoneInfo("Asia/Tokyo")

# --- 定数 ---
URL_REGEX = re.compile(r'https?://[^\s]+')
YOUTUBE_URL_REGEX = re.compile(r'(https?://(?:www\.youtube\.com/watch\?v=|youtu\.be/)[^\s]+)')
TITLE_LINK_EMOJI = "🔗"
CLIP_SUMMARY_EMOJI = "📎"
CLIP_SUCCESS_EMOJI = "✅"
CLIP_FAILED_EMOJI = "❌"


class MemoCog(commands.Cog):
    def __init__(self, bot, dbx, dropbox_vault_path):
        self.bot = bot
        self.dbx = dbx
        self.dropbox_vault_path = dropbox_vault_path
        self.pending_url_messages = {}  # {message_id: {"url": str, "is_youtube": bool}}

    # --- メッセージ受信時 ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        content = message.content.strip()
        if not content:
            return

        # --- URL検出 ---
        url_match = URL_REGEX.search(content)
        if url_match:
            url = url_match.group(0)
            logging.info(f"[memo_cog] URL detected in message {message.id}: {url}")

            youtube_match = YOUTUBE_URL_REGEX.search(url)
            is_youtube = bool(youtube_match)
            self.pending_url_messages[message.id] = {"url": url, "is_youtube": is_youtube}

            logging.info(f"[memo_cog] is_youtube={is_youtube}")
            try:
                await message.add_reaction(TITLE_LINK_EMOJI)
                await message.add_reaction(CLIP_SUMMARY_EMOJI)
            except discord.Forbidden:
                logging.error("Missing permissions to add reactions.")
            except discord.HTTPException as e:
                logging.error(f"Failed to add reactions: {e}")

    # --- リアクション追加時 ---
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return

        emoji = str(payload.emoji)
        if emoji not in [TITLE_LINK_EMOJI, CLIP_SUMMARY_EMOJI]:
            return

        info = self.pending_url_messages.pop(payload.message_id, None)
        if not info:
            return

        url = info["url"]
        is_youtube = info.get("is_youtube", False)

        channel = self.bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        user = self.bot.get_user(payload.user_id)

        try:
            if emoji == TITLE_LINK_EMOJI:
                await self._perform_title_link(message, url, is_youtube)
                await message.add_reaction(CLIP_SUCCESS_EMOJI)
            elif emoji == CLIP_SUMMARY_EMOJI:
                await self._perform_web_clip(message, url, is_youtube)
                await message.add_reaction(CLIP_SUCCESS_EMOJI)
        except Exception as e:
            logging.exception(f"[memo_cog] Reaction handling failed: {e}")
            await message.add_reaction(CLIP_FAILED_EMOJI)
            if user:
                await user.send(f"❌ エラーが発生しました: {e}")

    # --- YouTubeタイトル保存 ---
    async def _perform_title_link(self, message: discord.Message, url: str, is_youtube: bool):
        timestamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        note_title = f"{timestamp} | {url}"

        if is_youtube:
            logging.info(f"[memo_cog] Processing YouTube link: {url}")
            await append_youtube_summary_async(url)
            await append_text_to_doc_async(note_title, f"【YouTubeリンク】\n{url}")
        else:
            await append_text_to_doc_async(note_title, f"【リンク】\n{url}")

    # --- WebClip実行 ---
    async def _perform_web_clip(self, message: discord.Message, url: str, is_youtube: bool):
        if is_youtube:
            logging.info(f"[memo_cog] Web clip skipped (YouTube URL): {url}")
            await append_youtube_summary_async(url)
            return

        logging.info(f"[memo_cog] Starting WebClip: {url}")
        await message.channel.send(f"🌐 WebClipを開始します...\n{url}")

        try:
            # --- 本文抽出 ---
            parsed = await parse_url_with_readability(url)
            if not parsed or not parsed.get("title"):
                raise ValueError("本文の解析に失敗しました。")

            title = parsed["title"]
            content = parsed["content"]
            safe_title = re.sub(r'[\\/*?:"<>|]', '_', title).strip() or "webclip"

            timestamp = datetime.now(JST).strftime("%Y%m%d-%H%M%S")
            webclip_file_name = f"{timestamp}-{safe_title}.md"
            webclip_file_path = f"{self.dropbox_vault_path}/WebClips/{webclip_file_name}"

            webclip_note_content = f"# {title}\n\nURL: {url}\n\n---\n\n{content}"

            logging.info(f"[memo_cog] Uploading WebClip to Dropbox: {webclip_file_path}")

            # --- Dropboxアップロード（リトライ付き） ---
            for attempt in range(3):
                try:
                    await asyncio.to_thread(
                        self.dbx.files_upload,
                        webclip_note_content.encode("utf-8"),
                        webclip_file_path,
                        mode=WriteMode("add")
                    )
                    logging.info(f"[memo_cog] WebClip upload success: {webclip_file_path}")
                    break
                except ApiError as e:
                    if "conflict" in str(e).lower():
                        logging.warning(f"ファイル名競合 → リトライ中 ({attempt + 1}/3)")
                        timestamp = datetime.now(JST).strftime("%Y%m%d-%H%M%S")
                        webclip_file_name = f"{timestamp}-{safe_title}.md"
                        webclip_file_path = f"{self.dropbox_vault_path}/WebClips/{webclip_file_name}"
                    else:
                        raise

            # --- Obsidian更新 ---
            logging.info("[memo_cog] Updating Obsidian section...")
            update_result = await update_section("WebClip", f"- [[{webclip_file_name}]]")
            if update_result is None:
                raise ValueError("Obsidian更新に失敗しました。")

            await message.channel.send(f"✅ WebClipを保存しました: `{webclip_file_name}`")

        except Exception as e:
            logging.exception(f"[memo_cog] WebClip failed: {e}")
            await message.channel.send(f"❌ WebClip中にエラーが発生しました: {e}")


async def setup(bot):
    await bot.add_cog(MemoCog(bot, bot.dbx, bot.dropbox_vault_path))