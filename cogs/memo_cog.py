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
import aiohttp # URLチェック用に保持

# --- 共通処理インポート ---
from obsidian_handler import add_memo_async
# utils.obsidian_utils はこのファイルでは直接使わないため削除 (必要なら戻す)
# web_parser はこのファイルでは直接使わないため削除

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
# ★ 修正: ユーザーが付ける転送トリガー (WebClip, YouTube共通)
USER_TRANSFER_REACTION = '➡️' 
# Botが転送先で付けるリアクション (処理開始トリガー)
BOT_PROCESS_TRIGGER_REACTION = '📥'
# 処理ステータス用
PROCESS_FORWARDING_EMOJI = '➡️' # 転送処理中を示す
PROCESS_COMPLETE_EMOJI = '✅' # テキストメモ保存完了用
PROCESS_ERROR_EMOJI = '❌'

# URL Regex
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')
# YouTube URL Regex
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed|/youtube\.com/shorts/)([a-zA-Z0-9_-]{11})')


# Cog Class
class MemoCog(commands.Cog):
    """
    Discordの#memoチャンネルを監視し、テキストメモ保存、
    またはユーザーリアクション(➡️)に応じてURLを指定チャンネルに転送するCog
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logging.info("MemoCog: Initialized.") # 初期化ログ

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """#memo チャンネルに投稿されたメッセージを処理 (テキストメモのみ)"""
        if message.author.bot or message.channel.id != MEMO_CHANNEL_ID:
            return

        content = message.content.strip()
        if not content:
            return

        # ★ 修正: URLが含まれる場合は on_message では処理せず、リアクションを待つ
        url_match = URL_REGEX.search(content)
        if url_match:
            logging.info(f"URL detected in message {message.id}. Waiting for user reaction ({USER_TRANSFER_REACTION}).")
            return
        
        # 3. URLが含まれない場合: テキストメモとして保存
        logging.info(f"Text memo detected in message {message.id}. Saving via obsidian_handler.")
        try:
            await add_memo_async(
                content=content,
                author=str(message.author),
                created_at=message.created_at.isoformat(), # UTCのISOフォーマット
                message_id=message.id,
                context="Discord Memo Channel", # コンテキスト情報を追加
                category="Memo" # カテゴリ情報を追加
            )
            await message.add_reaction(PROCESS_COMPLETE_EMOJI) # 保存成功のリアクション
        except Exception as e:
            logging.error(f"Failed to save text memo (ID: {message.id}) using add_memo_async: {e}", exc_info=True)
            await message.add_reaction(PROCESS_ERROR_EMOJI)
        # ★ 修正ここまで

    # ★ 修正: 転送ロジックを共通関数化
    async def _forward_message(self, message: discord.Message, content_to_forward: str, target_channel_id: int, forward_type: str):
        """指定されたチャンネルにメッセージを転送し、トリガーリアクション(📥)を付与する"""
        if target_channel_id == 0:
            logging.warning(f"{forward_type} の転送先チャンネルIDが設定されていません。")
            return False

        # 既に転送処理中か確認
        if any(r.emoji == PROCESS_FORWARDING_EMOJI and r.me for r in message.reactions):
            logging.warning(f"メッセージ {message.id} は既に転送処理中です。スキップします。")
            return False

        # 転送中リアクションを追加
        try:
            await message.add_reaction(PROCESS_FORWARDING_EMOJI)
        except discord.HTTPException: pass

        # 転送先チャンネルを取得
        forward_channel = self.bot.get_channel(target_channel_id)
        if not forward_channel:
            logging.error(f"転送先チャンネル ID:{target_channel_id} が見つかりません。")
            await self._handle_forward_error(message)
            return False

        try:
            # --- メッセージ内容を転送先チャンネルに投稿 ---
            forwarded_message = await forward_channel.send(content_to_forward)
            logging.info(f"{forward_type} 用にメッセージ {message.id} をチャンネル '{forward_channel.name}' に転送しました (New ID: {forwarded_message.id})。")

            # --- ★ 修正: Botが 📥 リアクションを付与 ---
            await forwarded_message.add_reaction(BOT_PROCESS_TRIGGER_REACTION)
            logging.info(f"転送先メッセージ {forwarded_message.id} にトリガーリアクション {BOT_PROCESS_TRIGGER_REACTION} を追加しました。")
            # --- ★ 修正ここまで ---

            # 元のメッセージの転送中リアクションを削除
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
    # ★ 修正ここまで

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """ユーザーが付けたリアクション(➡️)に応じてURLメッセージを転送"""
        if payload.user_id == self.bot.user.id or payload.channel_id != MEMO_CHANNEL_ID:
            return

        emoji = str(payload.emoji)

        # ★ 修正: 転送トリガー (➡️) のみを処理
        if emoji != USER_TRANSFER_REACTION:
            return

        # 元のメッセージを取得
        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return
        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            logging.error(f"元のメッセージ {payload.message_id} の取得に失敗しました。")
            return

        # メッセージ内容がURLか確認
        content = message.content.strip()
        url_match = URL_REGEX.search(content)
        if not url_match:
            logging.warning(f"リアクション {emoji} がURLを含まないメッセージ {message.id} に追加されました。処理をスキップします。")
            try:
                user = await self.bot.fetch_user(payload.user_id)
                if user: await message.remove_reaction(payload.emoji, user)
            except discord.HTTPException: pass
            return
        
        # ユーザーリアクションを削除
        try:
            user = await self.bot.fetch_user(payload.user_id)
            if user:
                await message.remove_reaction(payload.emoji, user)
                logging.info(f"ユーザーリアクション {emoji} をメッセージ {message.id} から削除しました。")
        except discord.HTTPException:
            logging.warning(f"ユーザーリアクション {emoji} の削除に失敗: {message.id}")

        # ★ 修正: URLの種類を判別して転送先を決定
        youtube_url_match = YOUTUBE_URL_REGEX.search(content)
        if youtube_url_match:
            target_channel_id = YOUTUBE_SUMMARY_CHANNEL_ID
            forward_type = "YouTube Summary"
        else:
            target_channel_id = WEB_CLIP_CHANNEL_ID
            forward_type = "WebClip"

        # 共通の転送関数を呼び出す
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