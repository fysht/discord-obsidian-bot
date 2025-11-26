import os
import discord
from discord.ext import commands
import logging
import asyncio
from dotenv import load_dotenv

# --- 1. 設定読み込み ---
load_dotenv()
log_format = '%(asctime)s [%(levelname)s] [%(name)s] %(message)s'
logging.basicConfig(level=logging.INFO, format=log_format)

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
YOUTUBE_SUMMARY_CHANNEL_ID = int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0)) 

# レシピチャンネルIDはRender側で使うため、ここでは必須チェックしない（設定されていても使わない）

# --- 2. Botの定義 ---
class LocalWorkerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix="!local!", intents=intents)

    async def setup_hook(self):
        # YouTube要約機能のみをロード
        if YOUTUBE_SUMMARY_CHANNEL_ID:
            try:
                # RecipeCog は Render側で動かすためここではロードしない
                await self.load_extension("cogs.youtube_cog")
                logging.info("YouTubeCog loaded (Local Worker).")
            except Exception as e:
                logging.error(f"Failed to load YouTubeCog: {e}", exc_info=True)
        else:
            logging.warning("YOUTUBE_SUMMARY_CHANNEL_ID not set. YouTubeCog will not be loaded.")

    async def on_ready(self):
        logging.info(f"{self.user} としてログインしました (Local Worker - YouTube担当)")
        
        # 未処理スキャンの実行
        cog = self.get_cog('YouTubeCog')
        if cog and hasattr(cog, 'process_pending_summaries'):
            try:
                logging.info("Scanning pending items for YouTubeCog...")
                await cog.process_pending_summaries()
            except Exception as e:
                logging.error(f"Error in pending scan for YouTubeCog: {e}")

# --- 3. 起動処理 ---
async def main():
    if not TOKEN:
        logging.critical("DISCORD_BOT_TOKEN not found.")
        return
    
    # YouTubeチャンネルIDがない場合は起動する意味がないので警告
    if YOUTUBE_SUMMARY_CHANNEL_ID == 0:
        logging.critical("YOUTUBE_SUMMARY_CHANNEL_ID is not configured. Local worker has nothing to do.")
        return

    bot = LocalWorkerBot()
    try:
        await bot.start(TOKEN)
    except Exception as e:
         logging.critical(f"Bot crash: {e}", exc_info=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shutdown.")