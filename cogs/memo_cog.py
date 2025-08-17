import os
import discord
from discord.ext import commands
# obsidian_handlerから非同期版のadd_memo_asyncを読み込む
from obsidian_handler import add_memo_async

MEMO_CHANNEL_ID = int(os.getenv("MEMO_CHANNEL_ID", "0"))

class MemoCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Bot自身の発言は無視
        if message.author.bot:
            return

        # 指定されたメモチャンネル以外からのメッセージは無視
        if message.channel.id != MEMO_CHANNEL_ID:
            return

        # 非同期版のadd_memo_asyncを 'await' を付けて呼び出す
        await add_memo_async(
            message.content,
            author=f"{message.author} ({message.author.id})",
            created_at=message.created_at.isoformat()
        )
        
        # ユーザーに処理が完了したことを知らせるためにリアクションを付ける
        try:
            await message.add_reaction("✅")
        except discord.Forbidden:
            # リアクションの権限がない場合は何もしない
            pass

async def setup(bot):
    await bot.add_cog(MemoCog(bot))