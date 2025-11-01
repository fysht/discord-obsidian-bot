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
import re  # reをインポート
import aiohttp 

# --- 共通処理インポート ---
from obsidian_handler import add_memo_async
from web_parser import parse_url_with_readability # フォールバックとして保持

# --- 定数定義 ---
try:
    import zoneinfo
    JST = zoneinfo.ZoneInfo("Asia/Tokyo")
except ImportError:
    JST = timezone(timedelta(hours=+9), "JST")

# --- チャンネルID ---
MEMO_CHANNEL_ID = int(os.getenv("MEMO_CHANNEL_ID", 0))
WEB_CLIP_CHANNEL_ID = int(os.getenv("WEB_CLIP_CHANNEL_ID", 0))
YOUTUBE_SUMMARY_CHANNEL_ID = int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0))

# --- リアクション絵文字 ---
USER_TRANSFER_REACTION = '➡️' 
BOT_PROCESS_TRIGGER_REACTION = '📥'
PROCESS_FORWARDING_EMOJI = '➡️' 
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'
PROCESS_FETCHING_EMOJI = '⏱️' # 待機中

# URL Regex
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')
# YouTube URL Regex (★ youtube_cog.py からコピー)
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed|/youtube\.com/shorts/)([a-zA-Z0-9_-]{11})')


# Cog Class
class MemoCog(commands.Cog):
    """
    Discordの#memoチャンネルを監視し、テキストメモ保存、
    またはユーザーリアクション(➡️)に応じてURLを指定チャンネルに転送するCog
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session = aiohttp.ClientSession() 
        logging.info("MemoCog: Initialized.")

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def get_video_info(self, video_id: str) -> dict:
        """YouTube OEmbed APIを使用して動画情報を取得する"""
        url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36'}
            async with self.session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    try:
                        data = await response.json()
                        title = data.get("title")
                        author_name = data.get("author_name")
                        if title and author_name:
                            return {"title": title, "author_name": author_name}
                    except aiohttp.ContentTypeError: pass # エラーログは省略
            # 失敗した場合のフォールバック
            return {"title": f"YouTube_{video_id}", "author_name": "N/A"}
        except Exception as e:
            logging.warning(f"OEmbed unexpected error for {video_id}: {e}")
            return {"title": f"YouTube_{video_id}", "author_name": "N/A"}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """#memo チャンネルに投稿されたメッセージを処理 (テキストとURLの両方)"""
        if message.author.bot or message.channel.id != MEMO_CHANNEL_ID:
            return

        content = message.content.strip()
        if not content:
            return

        url_match = URL_REGEX.search(content)
        
        if url_match:
            logging.info(f"URL detected in message {message.id}. Saving as simple bookmark memo.")
            try:
                # ★ 修正: 待機中リアクション
                await message.add_reaction(PROCESS_FETCHING_EMOJI) 
            except discord.HTTPException: pass

            url = url_match.group(0)
            title = "タイトル不明" # デフォルト
            
            try:
                # --- ★ 修正: タイトル取得ロジックの変更 ---
                youtube_url_match = YOUTUBE_URL_REGEX.search(url)
                
                if youtube_url_match:
                    # 1. YouTubeリンクの場合 (OEmbed API)
                    logging.info(f"Fetching YouTube title (OEmbed) for {url}...")
                    video_id = youtube_url_match.group(1)
                    video_info = await self.get_video_info(video_id)
                    title = video_info.get("title", f"YouTube Video (ID: {video_id})")
                
                else:
                    # 2. 一般的なWebリンクの場合 (Discord Embedを待機)
                    logging.info(f"Waiting for Discord embed for {url}...")
                    await asyncio.sleep(5) # 5秒待機
                    
                    try:
                        # メッセージを再取得して embeds を確認
                        fetched_message = await message.channel.fetch_message(message.id)
                        if fetched_message.embeds:
                            embed_title = fetched_message.embeds[0].title
                            if embed_title and embed_title != discord.Embed.Empty:
                                title = embed_title
                                logging.info(f"Title found via Discord embed: {title}")
                    except (discord.NotFound, discord.Forbidden) as e:
                         logging.warning(f"Failed to re-fetch message {message.id} for embed: {e}")
                    
                    # 3. Embedが取得できなかった場合 (フォールバック)
                    if title == "タイトル不明":
                        logging.info(f"No Discord embed. Falling back to web_parser for {url}...")
                        loop = asyncio.get_running_loop()
                        parsed_title, _ = await loop.run_in_executor(
                            None, parse_url_with_readability, url
                        )
                        if parsed_title and parsed_title != "No Title Found":
                            title = parsed_title
                            logging.info(f"Title found via web_parser: {title}")
                        else:
                             logging.warning(f"web_parser also failed for {url}")
                # --- ★ 修正ここまで ---

                memo_content_to_save = f"{title}\n{url}"

                await add_memo_async(
                    content=memo_content_to_save,
                    author=str(message.author),
                    created_at=message.created_at.isoformat(),
                    message_id=message.id,
                    context="Discord Memo Channel (URL Bookmark)", 
                    category="Memo" 
                )
                
                await message.remove_reaction(PROCESS_FETCHING_EMOJI, self.bot.user)
                await message.add_reaction(PROCESS_COMPLETE_EMOJI) 
                logging.info(f"Successfully saved URL bookmark (ID: {message.id}), Title: {title}")
            
            except Exception as e:
                logging.error(f"Failed to parse URL title or save bookmark (ID: {message.id}): {e}", exc_info=True)
                try:
                    await message.remove_reaction(PROCESS_FETCHING_EMOJI, self.bot.user)
                    await message.add_reaction(PROCESS_ERROR_EMOJI)
                except discord.HTTPException: pass
            
        else:
            # URLが含まれない場合 (元のロジック)
            logging.info(f"Text memo detected in message {message.id}. Saving via obsidian_handler.")
            try:
                await add_memo_async(
                    content=content,
                    author=str(message.author),
                    created_at=message.created_at.isoformat(), 
                    message_id=message.id,
                    context="Discord Memo Channel", 
                    category="Memo" 
                )
                await message.add_reaction(PROCESS_COMPLETE_EMOJI) 
            except Exception as e:
                logging.error(f"Failed to save text memo (ID: {message.id}) using add_memo_async: {e}", exc_info=True)
                await message.add_reaction(PROCESS_ERROR_EMOJI)

    # ( _forward_message と _handle_forward_error は変更なし )
    async def _forward_message(self, message: discord.Message, content_to_forward: str, target_channel_id: int, forward_type: str):
        if target_channel_id == 0:
            logging.warning(f"{forward_type} の転送先チャンネルIDが設定されていません。")
            return False

        if any(r.emoji == PROCESS_FORWARDING_EMOJI and r.me for r in message.reactions):
            logging.warning(f"メッセージ {message.id} は既に転送処理中です。スキップします。")
            return False

        try:
            await message.add_reaction(PROCESS_FORWARDING_EMOJI)
        except discord.HTTPException: pass

        forward_channel = self.bot.get_channel(target_channel_id)
        if not forward_channel:
            logging.error(f"転送先チャンネル ID:{target_channel_id} が見つかりません。")
            await self._handle_forward_error(message)
            return False

        try:
            forwarded_message = await forward_channel.send(content_to_forward)
            logging.info(f"{forward_type} 用にメッセージ {message.id} をチャンネル '{forward_channel.name}' に転送しました (New ID: {forwarded_message.id})。")

            await forwarded_message.add_reaction(BOT_PROCESS_TRIGGER_REACTION)
            logging.info(f"転送先メッセージ {forwarded_message.id} にトリガーリアクション {BOT_PROCESS_TRIGGER_REACTION} を追加しました。")

            try: await message.remove_reaction(PROCESS_FORWARDING_EMOJI, self.bot.user)
            except discord.HTTPException: pass
            
            return True

        except discord.Forbidden:
            logging.error(f"チャンネル '{forward_channel.name}' (ID:{target_channel_id}) への投稿権限がありません。")
            await self._handle_forward_error(message)
            return False
        except discord.HTTPException as e:
            logging.error(f"メッセージの転送またはリアクション追加中にHTTPエラーが発生: {e}")
            await self._handle_forward_error(message)
            return False
        except Exception as e:
            logging.error(f"予期せぬ転送エラーが発生しました: {e}", exc_info=True)
            await self._handle_forward_error(message)
            return False

    async def _handle_forward_error(self, message: discord.Message):
        """転送エラー時のリアクション処理"""
        try: await message.remove_reaction(PROCESS_FORWARDING_EMOJI, self.bot.user)
        except discord.HTTPException: pass
        try: await message.add_reaction(PROCESS_ERROR_EMOJI)
        except discord.HTTPException: pass
    
    # ( on_raw_reaction_add は変更なし )
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """ユーザーが付けたリアクション(➡️)に応じてURLメッセージを転送"""
        if payload.user_id == self.bot.user.id or payload.channel_id != MEMO_CHANNEL_ID:
            return

        emoji = str(payload.emoji)

        if emoji != USER_TRANSFER_REACTION:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return
        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            logging.error(f"元のメッセージ {payload.message_id} の取得に失敗しました。")
            return

        content = message.content.strip()
        url_match = URL_REGEX.search(content)
        if not url_match:
            logging.warning(f"リアクション {emoji} がURLを含まないメッセージ {message.id} に追加されました。処理をスキップします。")
            try:
                user = await self.bot.fetch_user(payload.user_id)
                if user: await message.remove_reaction(payload.emoji, user)
            except discord.HTTPException: pass
            return
        
        try:
            user = await self.bot.fetch_user(payload.user_id)
            if user:
                await message.remove_reaction(payload.emoji, user)
                logging.info(f"ユーザーリアクション {emoji} をメッセージ {message.id} から削除しました。")
        except discord.HTTPException:
            logging.warning(f"ユーザーリアクション {emoji} の削除に失敗: {message.id}")

        youtube_url_match = YOUTUBE_URL_REGEX.search(content)
        if youtube_url_match:
            target_channel_id = YOUTUBE_SUMMARY_CHANNEL_ID
            forward_type = "YouTube Summary"
        else:
            target_channel_id = WEB_CLIP_CHANNEL_ID
            forward_type = "WebClip"

        await self._forward_message(message, content, target_channel_id, forward_type)


async def setup(bot: commands.Bot):
    """Cogセットアップ"""
    if MEMO_CHANNEL_ID == 0:
        logging.error("MemoCog: MEMO_CHANNEL_ID が設定されていません。Cogをロードしません。")
        return
    if WEB_CLIP_CHANNEL_ID == 0:
        logging.warning("MemoCog: WEB_CLIP_CHANNEL_ID が設定されていません。WebClipの転送は無効になります。")
    if YOUTUBE_SUMMARY_CHANNEL_ID == 0:
        logging.warning("MemoCog: YOUTUBE_SUMMARY_CHANNEL_ID が設定されていません。YouTubeの自動転送は無効になります。")

    await bot.add_cog(MemoCog(bot))