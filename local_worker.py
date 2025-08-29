import os
import discord
from discord.ext import commands
import logging
import asyncio
from dotenv import load_dotenv

# --- 1. 設定読み込み ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
# youtube_cogをインポートするために必要
from cogs.youtube_cog import YouTubeCog

# --- 2. Botの定義 ---
class LocalWorkerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        intents.guilds = True
        super().__init__(command_prefix="!local!", intents=intents)

    async def setup_hook(self):
        # 必要なCogだけをロード
        await self.add_cog(YouTubeCog(self))
        logging.info("YouTubeCogを読み込みました。")

    async def on_ready(self):
        logging.info(f"{self.user} としてログインしました (Local - 処理担当)")

        youtube_cog = self.get_cog('YouTubeCog')
        if youtube_cog:
            await youtube_cog.process_pending_summaries()
        else:
            logging.error("YouTubeCogの読み込みに失敗しました。")

        logging.info("すべての処理が完了しました。シャットダウンします。")
        await self.close()

# --- 3. 起動処理 ---
async def main():
    bot = LocalWorkerBot()
    await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("手動でシャットダウンしました。")