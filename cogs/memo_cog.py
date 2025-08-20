import discord
from discord.ext import commands
import logging
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
                author=str(message.author),
                content=message.content,
                message_id=str(message.id)
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
        logging.info("[memo_cog] Bot is ready. Checking missed messages...")

        for guild in self.bot.guilds:
            for channel in guild.text_channels:
                try:
                    async for message in channel.history(limit=50, oldest_first=False):
                        if message.author.bot:
                            continue

                        await add_memo_async(
                            author=str(message.author),
                            content=message.content,
                            message_id=str(message.id)
                        )
                        logging.info(f"[memo_cog] Backfilled memo: {message.content}")

                        # ✅ リアクション二重防止
                        for reaction in message.reactions:
                            if str(reaction.emoji) == "✅" and reaction.me:
                                break
                        else:
                            await message.add_reaction("✅")

                except discord.Forbidden:
                    logging.warning(f"[memo_cog] Cannot access channel: {channel.name}")
                except Exception as e:
                    logging.error(f"[memo_cog] Error while backfilling: {e}", exc_info=True)


async def setup(bot):
    await bot.add_cog(MemoCog(bot))