import os
import discord
from discord.ext import commands
import logging
import re
from discord import app_commands

# --- 定数定義 ---
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})')
BOT_PROCESS_TRIGGER_REACTION = '📥' 

class ReceptionCog(commands.Cog, name="ReceptionCog"):
    """
    YouTubeのURL投稿を監視し、処理待ちのリアクション(📥)を付ける受付係Cog
    (local_worker.py でロードされる)
    """
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.youtube_summary_channel_id = int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0))
        # ★ レシピチャンネルIDも取得
        self.recipe_channel_id = int(os.getenv("RECIPE_CHANNEL_ID", 0))
        # ★ 監視対象チャンネルのリストを作成
        self.watched_channels = {self.youtube_summary_channel_id, self.recipe_channel_id}
        if 0 in self.watched_channels:
             logging.warning("ReceptionCog: 監視対象チャンネルID(0)が含まれています。")
             self.watched_channels.remove(0) # 0が設定されていたら除外

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """指定されたチャンネルのメッセージを監視"""
        
        # ★ 監視対象チャンネルリストで判定
        if message.author.bot or message.channel.id not in self.watched_channels:
            return

        url_match = YOUTUBE_URL_REGEX.search(message.content)
        
        # YouTube URLが含まれているか
        if url_match:
            # 既にBotが 📥 を付けていないかチェック
            # (r.me は local_worker bot 自身を指す)
            is_processed = any(r.emoji == BOT_PROCESS_TRIGGER_REACTION and r.me for r in message.reactions)
            
            if not is_processed:
                logging.info(f"[ReceptionCog] URLを検知し、リアクションを付与: {message.jump_url}")
                try:
                    # 📥 を付与 (これが youtube_cog のトリガーになる)
                    await message.add_reaction(BOT_PROCESS_TRIGGER_REACTION)
                except Exception as e:
                    logging.error(f"[ReceptionCog] リアクション付与中にエラー: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(ReceptionCog(bot))