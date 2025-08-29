import os
import discord
from discord.ext import commands
import re
import logging

# --- 定数定義 ---
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})')

class ReceptionCog(commands.Cog):
    """YouTubeのURL投稿を監視し、処理待ちのリアクションを付ける受付係Cog"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.youtube_summary_channel_id = int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # リアクションによるトリガーに変更したため、このリスナーは不要になります。
        # 意図しない動作を防ぐため、passを記述しておきます。
        pass

async def setup(bot: commands.Bot):
    await bot.add_cog(ReceptionCog(bot))