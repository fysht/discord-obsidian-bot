import logging
from discord.ext import commands
from obsidian_handler import add_memo_async

logger = logging.getLogger(__name__)

class MemoCog(commands.Cog):
    """メモを監視して保存するCog"""

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return

        try:
            await add_memo_async(
                author=f"{message.author} ({message.author.id})",
                content=message.content,
                message_id=message.id,
                created_at=message.created_at.isoformat()
            )
            logger.info(f"Memo saved: {message.content[:30]}...")
        except Exception as e:
            logger.error(f"[MemoCog] メモ保存中にエラー発生: {e}", exc_info=True)


async def setup(bot):
    await bot.add_cog(MemoCog(bot))