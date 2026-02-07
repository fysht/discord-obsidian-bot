import os
import discord
from discord.ext import commands
import logging
from datetime import datetime
import zoneinfo

# obsidian_handler から非同期保存関数をインポート
try:
    from obsidian_handler import add_memo_async
except ImportError:
    logging.error("MemoCog: obsidian_handler.pyが見つかりません。")
    add_memo_async = None

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

class MemoCog(commands.Cog):
    """
    Discordのメモチャンネルへの投稿を、Obsidianのデイリーノートに転記するCog。
    実際の同期は sync_worker が行います。
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))

        if not add_memo_async:
            logging.warning("MemoCog: add_memo_async が利用できないため、メモ機能は動作しません。")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """メモチャンネルへの投稿を監視して保存"""
        if message.author.bot:
            return
        if message.channel.id != self.memo_channel_id:
            return
        
        content = message.content.strip()
        if not content:
            return

        # コマンドは無視
        if content.startswith("!") or content.startswith("/"):
            return

        if add_memo_async:
            try:
                # メモを保存待ちキューに追加
                # カテゴリやコンテキストが必要な場合は引数で渡せます
                await add_memo_async(
                    content=content,
                    author=message.author.display_name,
                    created_at=message.created_at.isoformat(),
                    message_id=message.id,
                    context="MemoChannel"
                )
                await message.add_reaction("📝")
            except Exception as e:
                logging.error(f"MemoCog: Save Error: {e}", exc_info=True)
                await message.add_reaction("❌")
        else:
             logging.error("MemoCog: Handler not available.")

async def setup(bot: commands.Bot):
    if int(os.getenv("MEMO_CHANNEL_ID", 0)) == 0:
        logging.warning("MemoCog: MEMO_CHANNEL_IDが設定されていないため、ロードされません。")
        return
    await bot.add_cog(MemoCog(bot))