import discord
from discord.ext import commands
import logging
from datetime import timezone
from obsidian_handler import add_memo_async


class MemoCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        try:
            await add_memo_async(
                author=f"{message.author} ({message.author.id})",
                content=message.content,
                message_id=str(message.id),
                created_at=message.created_at.replace(tzinfo=timezone.utc).isoformat()
            )
            logging.info(f"[memo_cog] Memo saved: {message.content}")

            # ✅ リアクション二重防止
            for reaction in message.reactions:
                if str(reaction.emoji) == "✅" and reaction.me:
                    logging.info("[memo_cog] Reaction already exists, skipping.")
                    return

            await message.add_reaction("✅")

        except Exception as e:
            logging.error(f"[memo_cog] Failed to save memo: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_ready(self):
        logging.info("[memo_cog] Bot is ready.")

async def setup(bot):
    await bot.add_cog(MemoCog(bot))