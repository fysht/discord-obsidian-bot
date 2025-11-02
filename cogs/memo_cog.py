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
# ★ 新規追加: 読書ノートチャンネルID
BOOK_NOTE_CHANNEL_ID = int(os.getenv("BOOK_NOTE_CHANNEL_ID", 0))


# --- リアクション絵文字 ---
USER_TRANSFER_REACTION = '➡️' 
# ★ 新規追加: 読書ノートトリガー
BOOK_NOTE_REACTION = '📖' 
BOT_PROCESS_TRIGGER_REACTION = '📥'
PROCESS_FORWARDING_EMOJI = '➡️' 
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'
PROCESS_FETCHING_EMOJI = '⏱️' # 待機中

# URL Regex
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')
# YouTube URL Regex (転送先の判別のみに使用)
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed|/youtube\.com/shorts/)([a-zA-Z0-9_-]{11})')


# Cog Class
class MemoCog(commands.Cog):
    """
    Discordの#memoチャンネルを監視し、テキストメモ保存、
    またはユーザーリアクション(➡️, 📖)に応じてURLを指定チャンネルに転送するCog
    """
    # ... (変更なし) ...
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session = aiohttp.ClientSession() 
        logging.info("MemoCog: Initialized.")

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()

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
                await message.add_reaction(PROCESS_FETCHING_EMOJI) 
            except discord.HTTPException: pass

            url_from_content = url_match.group(0) # メッセージ本文から取得したURL (途切れている可能性)
            url_to_save = url_from_content      # 最終的に保存するURL
            title = "タイトル不明"               # 最終的に保存するタイトル
            
            try:
                # --- ★ 修正: Discord Embedの待機と取得 (YouTube/Web/Book共通) ---
                logging.info(f"Waiting 7s for Discord embed for {url_from_content}...")
                await asyncio.sleep(7) # 埋め込みプレビューの生成を待機
                
                full_url_from_embed = None
                title_from_embed = None
                
                try:
                    # メッセージを再取得して embeds を確認
                    fetched_message = await message.channel.fetch_message(message.id)
                    if fetched_message.embeds:
                        embed = fetched_message.embeds[0]
                        
                        # 完全なURLを embed.url から取得
                        if embed.url:
                            full_url_from_embed = embed.url
                            logging.info(f"Full URL found via embed.url: {full_url_from_embed}")
                            
                        # 完全なタイトルを embed.title から取得
                        if embed.title:
                            title_from_embed = embed.title
                            logging.info(f"Title found via embed.title: {title_from_embed}")
                            
                except (discord.NotFound, discord.Forbidden) as e:
                     logging.warning(f"Failed to re-fetch message {message.id} for embed: {e}")
                
                # --- 保存するURLとタイトルの決定 ---
                
                # URL: embed.url があれば最優先、なければ本文のURL
                if full_url_from_embed:
                    url_to_save = full_url_from_embed
                
                # タイトル: embed.title があれば最優先
                # (ただし、タイトルがURLそのものである場合を除く = プレビュー失敗時)
                if title_from_embed and "http" not in title_from_embed:
                    title = title_from_embed
                else:
                    # Embedが取得できなかった場合 (フォールバック)
                    logging.info(f"Embed title unusable ('{title_from_embed}'). Falling back to web_parser for {url_to_save}...")
                    loop = asyncio.get_running_loop()
                    parsed_title, _ = await loop.run_in_executor(
                        None, parse_url_with_readability, url_to_save # (完全かもしれない) url_to_save を使用
                    )
                    if parsed_title and parsed_title != "No Title Found":
                        title = parsed_title
                        logging.info(f"Title found via web_parser: {title}")
                    else:
                         logging.warning(f"web_parser also failed for {url_to_save}")
                         if title_from_embed: # 最後の手段 (タイトルがURLでも採用)
                             title = title_from_embed
                # --- ★ 修正ここまで ---

                memo_content_to_save = f"{title}\n{url_to_save}"

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
                logging.info(f"Successfully saved URL bookmark (ID: {message.id}), Title: {title}, URL: {url_to_save}")
            
            except Exception as e:
                logging.error(f"Failed to parse URL title or save bookmark (ID: {message.id}): {e}", exc_info=True)
                try:
                    await message.remove_reaction(PROCESS_FETCHING_EMOJI, self.bot.user)
                    await message.add_reaction(PROCESS_ERROR_EMOJI)
                except discord.HTTPException: pass
            
        else:
            # URLが含まれない場合
            # ... (変更なし) ...
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

    async def _forward_message(self, message: discord.Message, content_to_forward: str, target_channel_id: int, forward_type: str):
        # ... (変更なし) ...
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
        # ... (変更なし) ...
        try: await message.remove_reaction(PROCESS_FORWARDING_EMOJI, self.bot.user)
        except discord.HTTPException: pass
        try: await message.add_reaction(PROCESS_ERROR_EMOJI)
        except discord.HTTPException: pass
    
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """ユーザーが付けたリアクション(➡️, 📖)に応じてURLメッセージを転送"""
        if payload.user_id == self.bot.user.id or payload.channel_id != MEMO_CHANNEL_ID:
            return

        emoji = str(payload.emoji)

        # ★ 修正: 監視対象の絵文字を増やす
        if emoji not in [USER_TRANSFER_REACTION, BOOK_NOTE_REACTION]:
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
            logging.warning(f"リアクション {emoji} がURLを含ないメッセージ {message.id} に追加されました。処理をスキップします。")
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

        
        # 転送するURLも、Discordの埋め込み(embed.url)から取得した完全なものを優先する
        final_url_to_forward = url_match.group(0) # デフォルト
        
        try:
            if message.embeds and message.embeds[0].url:
                final_url_to_forward = message.embeds[0].url
                logging.info(f"Forwarding with full URL from embed: {final_url_to_forward}")
            else:
                logging.warning(f"No embed.url found for forwarding message {message.id}, using original content.")
                # ★ 修正: Embedがない場合は元のメッセージ(URLのみのはず)をそのまま使う
                final_url_to_forward = content 
        except Exception as e:
            logging.warning(f"Could not get embed.url for forwarding message {message.id}: {e}. Using original content.")
            final_url_to_forward = content # フォールバック

        # ★ 修正: リアクションによって転送先を分岐
        if emoji == USER_TRANSFER_REACTION: # ➡️ の場合
            # 転送先の判別
            youtube_url_match = YOUTUBE_URL_REGEX.search(final_url_to_forward)
            if youtube_url_match:
                target_channel_id = YOUTUBE_SUMMARY_CHANNEL_ID
                forward_type = "YouTube Summary"
            else:
                target_channel_id = WEB_CLIP_CHANNEL_ID
                forward_type = "WebClip"
            
            await self._forward_message(message, final_url_to_forward, target_channel_id, forward_type)

        elif emoji == BOOK_NOTE_REACTION: # 📖 の場合
            target_channel_id = BOOK_NOTE_CHANNEL_ID
            forward_type = "Book Note"
            await self._forward_message(message, final_url_to_forward, target_channel_id, forward_type)
        # ★ 修正ここまで

async def setup(bot: commands.Bot):
    """Cogセットアップ"""
    if MEMO_CHANNEL_ID == 0:
        logging.error("MemoCog: MEMO_CHANNEL_ID が設定されていません。Cogをロードしません。")
        return
    if WEB_CLIP_CHANNEL_ID == 0:
        logging.warning("MemoCog: WEB_CLIP_CHANNEL_ID が設定されていません。WebClipの転送は無効になります。")
    if YOUTUBE_SUMMARY_CHANNEL_ID == 0:
        logging.warning("MemoCog: YOUTUBE_SUMMARY_CHANNEL_ID が設定されていません。YouTubeの自動転送は無効になります。")
    # ★ 新規追加: 読書ノートチャンネルの警告
    if BOOK_NOTE_CHANNEL_ID == 0:
        logging.warning("MemoCog: BOOK_NOTE_CHANNEL_ID が設定されていません。読書ノートの転送は無効になります。")

    await bot.add_cog(MemoCog(bot))