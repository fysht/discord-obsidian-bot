import discord
from discord.ext import commands
import logging
from obsidian_handler import add_memo_async


class MemoCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Bot自身のメッセージは無視
        if message.author.bot:
            return

        # --- デバッグログ出力 ---
        logging.info(
            f"[on_message] triggered | "
            f"id={message.id} | author={message.author} "
            f"| channel={message.channel} | content={message.content}"
        )

        # メモ保存処理
        await add_memo_async(str(message.author), message.content)


async def setup(bot):
    await bot.add_cog(MemoCog(bot))