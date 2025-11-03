import os
import discord
from discord.ext import commands
import asyncio
import logging
import dropbox  # ★ 追加
from dropbox.files import WriteMode, DownloadError  # ★ 追加
from dropbox.exceptions import ApiError  # ★ 追加
from datetime import datetime, timezone, timedelta
import json  # ★ 追加
import re
import aiohttp 

# --- 共通処理インポート ---
from obsidian_handler import add_memo_async
from web_parser import parse_url_with_readability

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
BOOK_NOTE_CHANNEL_ID = int(os.getenv("BOOK_NOTE_CHANNEL_ID", 0))
RECIPE_CHANNEL_ID = int(os.getenv("RECIPE_CHANNEL_ID", 0))

# --- リアクション絵文字 ---
USER_TRANSFER_REACTION = '➡️' 
BOOK_NOTE_REACTION = '📖' 
RECIPE_REACTION = '🍳'
BOT_PROCESS_TRIGGER_REACTION = '📥'
PROCESS_FORWARDING_EMOJI = '➡️' 
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'
PROCESS_FETCHING_EMOJI = '⏱️' 

# ★ 新規追加: ピン留めニュース機能用
PINNED_NEWS_REACTION = '📰'
PINNED_NEWS_JSON_PATH = f"{os.getenv('DROPBOX_VAULT_PATH', '/ObsidianVault')}/.bot/pinned_news_memos.json"
# ★ ここまで

# URL Regex
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')
# YouTube URL Regex (転送先の判別のみに使用)
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed|/youtube\.com/shorts/)([a-zA-Z0-9_-]{11})')


# Cog Class
class MemoCog(commands.Cog):
    """
    Discordの#memoチャンネルを監視し、テキストメモ保存、
    またはユーザーリアクション(➡️, 📖, 🍳, 📰)に応じて処理を分岐するCog
    """
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session = aiohttp.ClientSession() 
        
        # ★ 追加: Dropboxクライアントの初期化 (ニュースピン留め機能用)
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dbx = None
        self.pinned_news_lock = asyncio.Lock() # JSONファイルのRMW操作を保護

        if all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            try:
                self.dbx = dropbox.Dropbox(
                    oauth2_refresh_token=self.dropbox_refresh_token,
                    app_key=self.dropbox_app_key,
                    app_secret=self.dropbox_app_secret
                )
                self.dbx.users_get_current_account() # 接続テスト
                logging.info("MemoCog: Dropboxクライアント (ピン留めニュース用) が正常に初期化されました。")
            except Exception as e:
                logging.error(f"MemoCog: Dropboxクライアントの初期化に失敗: {e}")
                self.dbx = None
        else:
            logging.warning("MemoCog: Dropbox認証情報が不足しているため、ピン留めニュース機能(📰)は無効です。")
        # ★ ここまで

        logging.info("MemoCog: Initialized.")

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()

    # ★ 新規追加: ピン留めニュースJSONをDropboxから取得
    async def _get_pinned_news(self) -> list:
        """Dropboxからピン留めニュースのリストを取得する"""
        if not self.dbx: return []
        try:
            _, res = self.dbx.files_download(PINNED_NEWS_JSON_PATH)
            data = json.loads(res.content.decode('utf-8'))
            return data if isinstance(data, list) else []
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                logging.info(f"ピン留めニュースファイル ({PINNED_NEWS_JSON_PATH}) が見つかりません。新規作成します。")
                return []
            logging.error(f"ピン留めニュースの読み込みに失敗: {e}")
            return []
        except (json.JSONDecodeError, Exception) as e:
            logging.error(f"ピン留めニュースの解析に失敗: {e}")
            return []

    # ★ 新規追加: ピン留めニュースJSONをDropboxに保存
    async def _save_pinned_news(self, pinned_list: list):
        """Dropboxにピン留めニュースのリストを保存する"""
        if not self.dbx: return
        try:
            content = json.dumps(pinned_list, ensure_ascii=False, indent=2).encode('utf-8')
            self.dbx.files_upload(content, PINNED_NEWS_JSON_PATH, mode=WriteMode('overwrite'))
        except Exception as e:
            logging.error(f"ピン留めニュースの保存に失敗: {e}")

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

            url_from_content = url_match.group(0) 
            url_to_save = url_from_content      
            title = "タイトル不明"               
            
            try:
                # --- Discord Embedの待機と取得 (YouTube/Web/Book共通) ---
                logging.info(f"Waiting 7s for Discord embed for {url_from_content}...")
                await asyncio.sleep(7) 
                
                full_url_from_embed = None
                title_from_embed = None
                
                try:
                    fetched_message = await message.channel.fetch_message(message.id)
                    if fetched_message.embeds:
                        embed = fetched_message.embeds[0]
                        if embed.url:
                            full_url_from_embed = embed.url
                            logging.info(f"Full URL found via embed.url: {full_url_from_embed}")
                        if embed.title:
                            title_from_embed = embed.title
                            logging.info(f"Title found via embed.title: {title_from_embed}")
                except (discord.NotFound, discord.Forbidden) as e:
                     logging.warning(f"Failed to re-fetch message {message.id} for embed: {e}")
                
                # --- 保存するURLとタイトルの決定 ---
                if full_url_from_embed:
                    url_to_save = full_url_from_embed
                
                if title_from_embed and "http" not in title_from_embed:
                    title = title_from_embed
                else:
                    logging.info(f"Embed title unusable ('{title_from_embed}'). Falling back to web_parser for {url_to_save}...")
                    loop = asyncio.get_running_loop()
                    parsed_title, _ = await loop.run_in_executor(
                        None, parse_url_with_readability, url_to_save
                    )
                    if parsed_title and parsed_title != "No Title Found":
                        title = parsed_title
                        logging.info(f"Title found via web_parser: {title}")
                    else:
                         logging.warning(f"web_parser also failed for {url_to_save}")
                         if title_from_embed:
                             title = title_from_embed

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
    
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """ユーザーが付けたリアクション(➡️, 📖, 🍳, 📰)に応じて処理を分岐"""
        if payload.user_id == self.bot.user.id or payload.channel_id != MEMO_CHANNEL_ID:
            return

        emoji = str(payload.emoji)

        # ★ 修正: 監視対象の絵文字を増やす
        if emoji not in [USER_TRANSFER_REACTION, BOOK_NOTE_REACTION, RECIPE_REACTION, PINNED_NEWS_REACTION]:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return
        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            logging.error(f"元のメッセージ {payload.message_id} の取得に失敗しました。")
            return

        # ユーザーリアクションはすぐに削除
        try:
            user = await self.bot.fetch_user(payload.user_id)
            if user:
                await message.remove_reaction(payload.emoji, user)
                logging.info(f"ユーザーリアクション {emoji} をメッセージ {message.id} から削除しました。")
        except discord.HTTPException:
            logging.warning(f"ユーザーリアクション {emoji} の削除に失敗: {message.id}")

        content = message.content.strip()
        url_match = URL_REGEX.search(content)

        # --- 📰 (ピン留めニュース) 処理 ---
        if emoji == PINNED_NEWS_REACTION:
            if not self.dbx:
                logging.warning(f"ピン留めリアクション (📰) が押されましたが、Dropboxが未初期化のためスキップします (Msg: {message.id})")
                await message.add_reaction(PROCESS_ERROR_EMOJI)
                await asyncio.sleep(3)
                await message.remove_reaction(PROCESS_ERROR_EMOJI, self.bot.user)
                return

            async with self.pinned_news_lock:
                try:
                    pinned_list = await self._get_pinned_news()
                    
                    # 既に存在するかチェック
                    if any(item.get("id") == str(message.id) for item in pinned_list):
                        logging.warning(f"メッセージ {message.id} は既にピン留めされています。")
                        return

                    new_pin = {
                        "id": str(message.id),
                        "content": message.content, # メッセージ内容全体
                        "author": str(message.author),
                        "pinned_at": datetime.now(JST).isoformat()
                    }
                    pinned_list.append(new_pin)
                    await self._save_pinned_news(pinned_list)
                    
                    await message.add_reaction(PROCESS_COMPLETE_EMOJI) # 転送ではなく「完了」
                    logging.info(f"メッセージ {message.id} をピン留めニュースとして保存しました。")
                
                except Exception as e:
                    logging.error(f"ピン留めニュースの保存中にエラー: {e}", exc_info=True)
                    await self._handle_forward_error(message) # エラーリアクション
            return # 転送処理は行わないのでここで終了

        # --- 以下、従来の転送処理 (➡️, 📖, 🍳) ---
        
        if not url_match:
            logging.warning(f"転送リアクション {emoji} がURLを含ないメッセージ {message.id} に追加されました。処理をスキップします。")
            return
        
        final_url_to_forward = url_match.group(0) # デフォルト
        
        try:
            if message.embeds and message.embeds[0].url:
                final_url_to_forward = message.embeds[0].url
                logging.info(f"Forwarding with full URL from embed: {final_url_to_forward}")
            else:
                logging.warning(f"No embed.url found for forwarding message {message.id}, using original content.")
                final_url_to_forward = content 
        except Exception as e:
            logging.warning(f"Could not get embed.url for forwarding message {message.id}: {e}. Using original content.")
            final_url_to_forward = content 

        if emoji == USER_TRANSFER_REACTION: # ➡️ の場合
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
            
        elif emoji == RECIPE_REACTION: # 🍳 の場合
            target_channel_id = RECIPE_CHANNEL_ID
            forward_type = "Recipe"
            await self._forward_message(message, final_url_to_forward, target_channel_id, forward_type)

    # ★ 新規追加: ピン留め解除 (リアクション削除) の監視
    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        """ユーザーが 📰 リアクションを削除した際の処理"""
        if payload.user_id == self.bot.user.id or payload.channel_id != MEMO_CHANNEL_ID:
            return
        
        if str(payload.emoji) != PINNED_NEWS_REACTION:
            return
        
        if not self.dbx:
            logging.warning(f"ピン留め解除リアクション (📰) が検知されましたが、Dropboxが未初期化のためスキップします (Msg: {payload.message_id})")
            return
            
        logging.info(f"ピン留め解除リアクションを検知 (Msg: {payload.message_id})。")

        async with self.pinned_news_lock:
            try:
                pinned_list = await self._get_pinned_news()
                message_id_to_remove = str(payload.message_id)
                
                initial_count = len(pinned_list)
                filtered_list = [item for item in pinned_list if item.get("id") != message_id_to_remove]
                
                if len(filtered_list) < initial_count:
                    await self._save_pinned_news(filtered_list)
                    logging.info(f"メッセージ {message_id_to_remove} をピン留めニュースから削除しました。")
                    
                    # 元メッセージに一時的にリアクション
                    try:
                        channel = self.bot.get_channel(payload.channel_id)
                        if channel:
                            message = await channel.fetch_message(payload.message_id)
                            await message.add_reaction("🗑️")
                            await asyncio.sleep(5)
                            await message.remove_reaction("🗑️", self.bot.user)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                        logging.warning(f"ピン留め解除のリアクション操作に失敗: {e}")
                else:
                    logging.warning(f"ピン留め解除が要求されましたが、メッセージ {message_id_to_remove} はリストに見つかりませんでした。")
            
            except Exception as e:
                logging.error(f"ピン留めニュースの削除中にエラー: {e}", exc_info=True)
        # ★ ここまで


async def setup(bot: commands.Bot):
    """Cogセットアップ"""
    if MEMO_CHANNEL_ID == 0:
        logging.error("MemoCog: MEMO_CHANNEL_ID が設定されていません。Cogをロードしません。")
        return
    if WEB_CLIP_CHANNEL_ID == 0:
        logging.warning("MemoCog: WEB_CLIP_CHANNEL_ID が設定されていません。WebClipの転送は無効になります。")
    if YOUTUBE_SUMMARY_CHANNEL_ID == 0:
        logging.warning("MemoCog: YOUTUBE_SUMMARY_CHANNEL_ID が設定されていません。YouTubeの自動転送は無効になります。")
    if BOOK_NOTE_CHANNEL_ID == 0:
        logging.warning("MemoCog: BOOK_NOTE_CHANNEL_ID が設定されていません。読書ノートの転送は無効になります。")
    if RECIPE_CHANNEL_ID == 0:
        logging.warning("MemoCog: RECIPE_CHANNEL_ID が設定されていません。レシピの転送は無効になります。")

    await bot.add_cog(MemoCog(bot))