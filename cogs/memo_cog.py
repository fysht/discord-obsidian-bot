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
WEB_CLIP_FORWARD_CHANNEL_ID = int(os.getenv("WEB_CLIP_FORWARD_CHANNEL_ID", 0))
YOUTUBE_SUMMARY_FORWARD_CHANNEL_ID = int(os.getenv("YOUTUBE_SUMMARY_FORWARD_CHANNEL_ID", 0))

# --- リアクション絵文字 ---
# ユーザーが付けるリアクション (転送トリガー)
WEB_CLIP_USER_REACTION = '📄'
YOUTUBE_SUMMARY_USER_REACTION = '🎬'
# Botが転送先で付けるリアクション (処理開始トリガー)
BOT_PROCESS_TRIGGER_REACTION = '📥'
# 処理ステータス用
PROCESS_FORWARDING_EMOJI = '➡️'
PROCESS_COMPLETE_EMOJI = '✅' # テキストメモ保存完了用
PROCESS_ERROR_EMOJI = '❌'

# URL Regex
URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')

# Cog Class
class MemoCog(commands.Cog):
    """
    Discordの#memoチャンネルを監視し、テキストメモ保存、
    またはユーザーリアクションに応じてURLを指定チャンネルに転送するCog
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Dropboxクライアントはテキストメモ保存時には不要なため削除 (必要なら戻す)
        # self.dbx = None
        # self._initialize_dropbox()
        logging.info("MemoCog: Initialized.") # 初期化ログ

    # Dropbox初期化は不要なためコメントアウト (必要なら戻す)
    # def _initialize_dropbox(self):
    #     """Initialize Dropbox client from environment variables."""
    #     # ... (以前のコード) ...

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """#memo チャンネルに投稿されたメッセージを処理 (テキストのみ保存、URLは無視)"""
        if message.author.bot or message.channel.id != MEMO_CHANNEL_ID:
            return

        content = message.content.strip()
        if not content:
            return

        # URLが含まれているかチェック
        url_match = URL_REGEX.search(content)
        if url_match:
            # URLが含まれる場合は何もしない (リアクション待ち)
            logging.info(f"URL detected in message {message.id} in #memo. Waiting for user reaction ({WEB_CLIP_USER_REACTION} or {YOUTUBE_SUMMARY_USER_REACTION}).")
        else:
            # URLが含まれない場合はテキストメモとして保存
            logging.info(f"Text memo detected in message {message.id}. Saving via obsidian_handler.")
            try:
                # obsidian_handler を使って非同期で保存
                # add_memo_async に渡す引数を調整 (category, context を追加)
                await add_memo_async(
                    content=content,
                    author=str(message.author),
                    created_at=message.created_at.isoformat(), # UTCのISOフォーマット
                    message_id=message.id,
                    context="Discord Memo Channel", # コンテキスト情報を追加
                    category="Memo" # カテゴリ情報を追加
                )
                await message.add_reaction(PROCESS_COMPLETE_EMOJI) # 保存成功のリアクション
                # 一定時間後にリアクションを消す (任意)
                # asyncio.create_task(self.remove_reaction_after_delay(message, PROCESS_COMPLETE_EMOJI))
            except Exception as e:
                logging.error(f"Failed to save text memo (ID: {message.id}) using add_memo_async: {e}", exc_info=True)
                await message.add_reaction(PROCESS_ERROR_EMOJI)

    # リアクション削除用のヘルパー関数 (任意)
    # async def remove_reaction_after_delay(self, message: discord.Message, emoji: str, delay: int = 15):
    #     await asyncio.sleep(delay)
    #     try:
    #         await message.remove_reaction(emoji, self.bot.user)
    #     except discord.HTTPException:
    #         pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """ユーザーが付けたリアクションに応じてURLメッセージを転送"""
        # Bot自身のリアクション、対象外チャンネルは無視
        if payload.user_id == self.bot.user.id or payload.channel_id != MEMO_CHANNEL_ID:
            return

        emoji = str(payload.emoji)
        target_channel_id = 0
        forward_type = ""

        # ユーザーが付けたリアクションに応じて転送先を決定
        if emoji == WEB_CLIP_USER_REACTION:
            target_channel_id = WEB_CLIP_FORWARD_CHANNEL_ID
            forward_type = "WebClip"
        elif emoji == YOUTUBE_SUMMARY_USER_REACTION:
            target_channel_id = YOUTUBE_SUMMARY_FORWARD_CHANNEL_ID
            forward_type = "YouTube Summary"
        else:
            # 対象のリアクションでなければ何もしない
            return

        if target_channel_id == 0:
            logging.warning(f"{forward_type} の転送先チャンネルID (env: {forward_type.upper().replace(' ', '_')}_FORWARD_CHANNEL_ID) が設定されていません。")
            # ユーザーに通知するかどうか (任意)
            # channel = self.bot.get_channel(payload.channel_id)
            # if channel: await channel.send(f"エラー: {forward_type} の転送先が設定されていません。", delete_after=10)
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
            # ユーザーリアクションを削除 (任意)
            try:
                user = await self.bot.fetch_user(payload.user_id)
                if user: await message.remove_reaction(payload.emoji, user)
            except discord.HTTPException: pass
            return
        # URL全体を転送内容とする
        url_to_forward = content

        # 既に転送処理中か確認
        if any(r.emoji == PROCESS_FORWARDING_EMOJI and r.me for r in message.reactions):
            logging.warning(f"メッセージ {message.id} は既に転送処理中です。リアクション {emoji} を無視します。")
            # ユーザーリアクションを削除 (任意)
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

        # 転送中リアクションを追加
        try:
            await message.add_reaction(PROCESS_FORWARDING_EMOJI)
        except discord.HTTPException: pass

        # 転送先チャンネルを取得
        forward_channel = self.bot.get_channel(target_channel_id)
        if not forward_channel:
            logging.error(f"転送先チャンネル ID:{target_channel_id} が見つかりません。")
            try: await message.remove_reaction(PROCESS_FORWARDING_EMOJI, self.bot.user)
            except discord.HTTPException: pass
            try: await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass
            return

        try:
            # --- メッセージ内容を転送先チャンネルに投稿 ---
            # embedなどを使わず、URLテキストのみを送信
            forwarded_message = await forward_channel.send(url_to_forward)
            logging.info(f"{forward_type} 用にメッセージ {message.id} をチャンネル '{forward_channel.name}' に転送しました (New ID: {forwarded_message.id})。")

            # --- 新しく投稿したメッセージに処理開始トリガーリアクションを追加 ---
            await forwarded_message.add_reaction(BOT_PROCESS_TRIGGER_REACTION)
            logging.info(f"転送先メッセージ {forwarded_message.id} にトリガーリアクション {BOT_PROCESS_TRIGGER_REACTION} を追加しました。")

            # 元のメッセージの転送中リアクションを削除
            try: await message.remove_reaction(PROCESS_FORWARDING_EMOJI, self.bot.user)
            except discord.HTTPException: pass
            # 元のメッセージに転送完了を示すリアクションを追加 (任意)
            # await message.add_reaction('👍') # 例えばサムズアップ

        except discord.Forbidden:
            logging.error(f"チャンネル '{forward_channel.name}' (ID:{target_channel_id}) への投稿権限がありません。")
            try: await message.remove_reaction(PROCESS_FORWARDING_EMOJI, self.bot.user)
            except discord.HTTPException: pass
            try: await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass
        except discord.HTTPException as e:
            logging.error(f"メッセージの転送またはリアクション追加中にHTTPエラーが発生: {e}")
            try: await message.remove_reaction(PROCESS_FORWARDING_EMOJI, self.bot.user)
            except discord.HTTPException: pass
            try: await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass
        except Exception as e:
            logging.error(f"予期せぬ転送エラーが発生しました: {e}", exc_info=True)
            try: await message.remove_reaction(PROCESS_FORWARDING_EMOJI, self.bot.user)
            except discord.HTTPException: pass
            try: await message.add_reaction(PROCESS_ERROR_EMOJI)
            except discord.HTTPException: pass

async def setup(bot: commands.Bot):
    """Cogセットアップ"""
    if MEMO_CHANNEL_ID == 0:
        logging.error("MemoCog: MEMO_CHANNEL_ID が設定されていません。Cogをロードしません。")
        return

    await bot.add_cog(MemoCog(bot))