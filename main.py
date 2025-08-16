# main.py

import os
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

class MyBot(commands.Bot):
    """Botの本体クラス"""
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        """Botが起動準備を終えたときに呼び出される特別な関数"""
        print(f'{self.user} としてログインしました！')
        
        for filename in os.listdir('./cogs'):
            if filename.endswith('.py'):
                try:
                    await self.load_extension(f'cogs.{filename[:-3]}')
                    print(f'{filename} を読み込みました。')
                except Exception as e:
                    print(f'{filename} の読み込みに失敗しました: {e}')
        
        synced = await self.tree.sync()
        print(f'{len(synced)}個のスラッシュコマンドを同期しました。')

async def main():
    """Botを起動するためのメイン関数"""
    bot = MyBot()
    token = os.getenv('DISCORD_BOT_TOKEN')
    if token is None:
        print("エラー: DISCORD_BOT_TOKENが.envファイルに設定されていません。")
        return
        
    await bot.start(token)

if __name__ == "__main__":
    asyncio.run(main())