import os
import discord
from discord.ext import commands
import logging
from datetime import timezone
from obsidian_handler import add_memo_async

MEMO_CHANNEL_ID = int(os.getenv("MEMO_CHANNEL_ID", "0"))

class MemoCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
            
        if message.channel.id != MEMO_CHANNEL_ID:
            return

        try:
            await add_memo_async(
                content=message.content,
                author=f"{message.author} ({message.author.id})",
                created_at=message.created_at.replace(tzinfo=timezone.utc).isoformat(),
                message_id=message.id
            )
            await message.add_reaction("✅")
        except Exception as e:
            logging.error(f"[memo_cog] Failed to save memo: {e}", exc_info=True)
            try:
                await message.add_reaction("❌")
            except discord.Forbidden:
                pass

    @commands.Cog.listener()
    async def on_ready(self):
        logging.info("[memo_cog] Bot is ready.")

async def setup(bot):
    await bot.add_cog(MemoCog(bot))