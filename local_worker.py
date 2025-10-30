import os
import discord
from discord.ext import commands
import logging
import asyncio
from dotenv import load_dotenv

# --- 1. 設定読み込み ---
load_dotenv()
# --- 修正: ログフォーマットに 'name' (Cog名) を追加 ---
log_format = '%(asctime)s [%(levelname)s] [%(name)s] %(message)s'
logging.basicConfig(level=logging.INFO, format=log_format)
# --- 修正ここまで ---

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
YOUTUBE_SUMMARY_CHANNEL_ID = int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0)) 

# --- 2. Botの定義 ---
class LocalWorkerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True  # ★ Intents.reactions が必須
        intents.guilds = True
        super().__init__(command_prefix="!local!", intents=intents)
        self.youtube_cog = None

    async def setup_hook(self):
        # 必要なCogだけをロード
        try:
            await self.load_extension("cogs.youtube_cog")
            self.youtube_cog = self.get_cog('YouTubeCog')
            if self.youtube_cog:
                 logging.info("YouTubeCogを読み込み、インスタンスを取得しました。")
            else:
                 logging.error("YouTubeCogのインスタンス取得に失敗しました。")
        except Exception as e:
            logging.error(f"YouTubeCogの読み込みに失敗: {e}", exc_info=True)


    async def on_ready(self):
        logging.info(f"{self.user} としてログインしました (Local - YouTube処理担当)")

        if not self.youtube_cog:
             logging.error("YouTubeCogがロードされていないため、処理を開始できません。")
             return

        # --- 修正: 起動時の未処理スキャンを有効化 ---
        logging.info("起動時に未処理のリアクションをスキャンします...")
        try:
            if hasattr(self.youtube_cog, 'process_pending_summaries'):
                await self.youtube_cog.process_pending_summaries()
            else:
                logging.error("YouTubeCogに process_pending_summaries メソッドが見つかりません。")
        except Exception as e:
            logging.error(f"起動時のYouTube要約一括処理中にエラー: {e}", exc_info=True)
        # --- 修正ここまで ---

        logging.info(f"リアクション監視モードに移行します。（チャンネル {YOUTUBE_SUMMARY_CHANNEL_ID} の 📥 を待ち受けます）")

    # (local_worker.py 本体には on_raw_reaction_add は不要。cogs/youtube_cog.py が検知する)

# --- 3. 起動処理 ---
async def main():
    if not TOKEN:
        logging.critical("DISCORD_BOT_TOKENが設定されていません。ローカルワーカーを起動できません。")
        return
    if YOUTUBE_SUMMARY_CHANNEL_ID == 0:
        logging.critical("YOUTUBE_SUMMARY_CHANNEL_IDが設定されていません。ローカルワーカーを起動できません。")
        return

    bot = LocalWorkerBot()
    try:
        await bot.start(TOKEN)
    except discord.LoginFailure:
         logging.critical("Discordトークンが無効です。ローカルワーカーを起動できません。")
    except Exception as e:
         logging.critical(f"ローカルワーカーの起動中に致命的なエラーが発生しました: {e}", exc_info=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("ローカルワーカーを手動でシャットダウンしました。")