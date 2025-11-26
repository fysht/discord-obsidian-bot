import discord
from discord.ext import commands
from discord import app_commands
import os
import re
import logging

# --- 定数定義 ---
# YouTubeのURLパターン
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})')
# Botが付けるリアクション
BOT_PROCESS_TRIGGER_REACTION = '📥'

class ReceptionCog(commands.Cog):
    """
    YouTubeのURL投稿を監視し、処理待ちのリアクション(📥)を付ける受付係Cog
    (Render側で常時稼働し、重い処理は行わない)
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.youtube_summary_channel_id = int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0))
        self.recipe_channel_id = int(os.getenv("RECIPE_CHANNEL_ID", 0))
        
        # 監視対象チャンネルのリスト
        self.watched_channels = set()
        if self.youtube_summary_channel_id:
            self.watched_channels.add(self.youtube_summary_channel_id)
        
        # レシピチャンネルも同様にリアクション付与だけ行いたい場合は追加
        # if self.recipe_channel_id:
        #     self.watched_channels.add(self.recipe_channel_id)
            
        if 0 in self.watched_channels:
            self.watched_channels.remove(0)
            logging.warning("ReceptionCog: 監視対象チャンネルID(0)が含まれています。")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Bot自身のメッセージや、監視対象外のチャンネルは無視
        if message.author.bot:
            return
        
        if message.channel.id not in self.watched_channels:
            return

        # YouTubeのURLが含まれているかチェック
        if YOUTUBE_URL_REGEX.search(message.content):
            # 既にリアクションが付いているか確認（自分自身によるもの）
            already_reacted = False
            for reaction in message.reactions:
                if str(reaction.emoji) == BOT_PROCESS_TRIGGER_REACTION and reaction.me:
                    already_reacted = True
                    break
            
            if not already_reacted:
                try:
                    await message.add_reaction(BOT_PROCESS_TRIGGER_REACTION)
                    logging.info(f"[ReceptionCog] URLを検知し、リアクションを付与: {message.jump_url}")
                except discord.Forbidden:
                    logging.error(f"[ReceptionCog] リアクション付与権限がありません: {message.channel.name}")
                except Exception as e:
                    logging.error(f"[ReceptionCog] リアクション付与中にエラー: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(ReceptionCog(bot))