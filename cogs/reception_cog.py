import os
import discord
from discord.ext import commands
import re
import logging

# --- 定数定義 ---
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0--9_-]{11})')

class ReceptionCog(commands.Cog):
    """YouTubeのURL投稿を監視し、処理待ちのリアクションを付ける受付係Cog"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.youtube_summary_channel_id = int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # ボット自身のメッセージや、監視対象外のチャンネルは無視
        if message.author.bot or message.channel.id != self.youtube_summary_channel_id:
            return

        # メッセージにYouTubeのURLが含まれているかチェック
        if YOUTUBE_URL_REGEX.search(message.content):
            try:
                # 処理済みリアクションがなければ、処理待ちリアクションを付ける
                is_processed = any(r.emoji in ('✅', '❌', '⏳') and r.me for r in message.reactions)
                if not is_processed:
                    await message.add_reaction("📥")
                    logging.info(f"[ReceptionCog] URLを検知し、リアクションを付与: {message.jump_url}")
            except Exception as e:
                logging.error(f"[ReceptionCog] リアクション付与中にエラー: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(ReceptionCog(bot))