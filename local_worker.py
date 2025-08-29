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

# --- 2. Botの定義 ---
class LocalWorkerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True  # リアクションを検知するためにTrueである必要があります
        intents.guilds = True
        super().__init__(command_prefix="!local!", intents=intents)

    async def setup_hook(self):
        # 必要なCogだけをロード
        await self.load_extension("cogs.youtube_cog")
        logging.info("YouTubeCogを読み込みました。")

    async def on_ready(self):
        logging.info(f"{self.user} としてログインしました (Local - 処理担当)")
        
        # Cogを取得
        youtube_cog = self.get_cog('YouTubeCog')
        if youtube_cog:
            logging.info("起動時に未処理のリアクションをスキャンします...")
            try:
                # 起動時に一度だけ、既存のリアクションをまとめて処理する
                await youtube_cog.process_pending_summaries()
            except Exception as e:
                logging.error(f"起動時のYouTube要約一括処理中にエラー: {e}", exc_info=True)
        else:
            logging.error("YouTubeCogが見つかりません。起動時スキャンを中止します。")

        logging.info("リアクション監視モードに移行します。（新しいリアクションを待ち受けます）")


# --- 3. 起動処理 ---
async def main():
    bot = LocalWorkerBot()
    await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("手動でシャットダウンしました。")