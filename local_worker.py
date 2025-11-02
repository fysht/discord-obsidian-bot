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
# ★ レシピチャンネルIDも取得
RECIPE_CHANNEL_ID = int(os.getenv("RECIPE_CHANNEL_ID", 0)) 

# --- 2. Botの定義 ---
class LocalWorkerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True  # ★ Intents.reactions が必須
        intents.guilds = True     # ★ チャンネル取得やメンバー情報のために Guilds も推奨
        intents.members = True    # ★ payload.member を取得するために Members も推奨
        super().__init__(command_prefix="!local!", intents=intents)
        self.youtube_cog = None
        self.reception_cog = None # ★ 追加

    async def setup_hook(self):
        # 必要なCogだけをロード
        try:
            # ★ reception_cog もロードする
            await self.load_extension("cogs.reception_cog")
            self.reception_cog = self.get_cog('ReceptionCog')
            if self.reception_cog:
                 logging.info("ReceptionCogを読み込み、インスタンスを取得しました。")
            else:
                 logging.error("ReceptionCogのインスタンス取得に失敗しました。")

            await self.load_extension("cogs.youtube_cog")
            self.youtube_cog = self.get_cog('YouTubeCog')
            if self.youtube_cog:
                 logging.info("YouTubeCogを読み込み、インスタンスを取得しました。")
            else:
                 logging.error("YouTubeCogのインスタンス取得に失敗しました。")
                 
        except Exception as e:
            logging.error(f"Cogの読み込みに失敗: {e}", exc_info=True)


    async def on_ready(self):
        logging.info(f"{self.user} としてログインしました (Local - YouTube/Recipe[YT]処理担当)")

        if not self.youtube_cog or not self.reception_cog: # ★ 両方チェック
             logging.error("必要なCogがロードされていないため、処理を開始できません。")
             return

        # --- 起動時の未処理スキャンを有効化 ---
        logging.info("起動時に未処理のリアクションをスキャンします...")
        try:
            if hasattr(self.youtube_cog, 'process_pending_summaries'):
                # ★ process_pending_summaries に両方のチャンネルIDを渡す
                await self.youtube_cog.process_pending_summaries(
                    channel_id=YOUTUBE_SUMMARY_CHANNEL_ID, 
                    recipe_channel_id=RECIPE_CHANNEL_ID
                )
            else:
                logging.error("YouTubeCogに process_pending_summaries メソッドが見つかりません。")
        except Exception as e:
            logging.error(f"起動時のYouTube要約一括処理中にエラー: {e}", exc_info=True)
        # --- 修正ここまで ---

        logging.info(f"監視モードに移行します。（監視対象: {YOUTUBE_SUMMARY_CHANNEL_ID}, {RECIPE_CHANNEL_ID}）")

    # (on_raw_reaction_add は cogs/youtube_cog.py が検知)
    # (on_message は cogs/reception_cog.py が検知)

# --- 3. 起動処理 ---
async def main():
    if not TOKEN:
        logging.critical("DISCORD_BOT_TOKENが設定されていません。ローカルワーカーを起動できません。")
        return
    # ★ 両方のチャンネルIDをチェック
    if YOUTUBE_SUMMARY_CHANNEL_ID == 0 or RECIPE_CHANNEL_ID == 0:
        logging.critical("YOUTUBE_SUMMARY_CHANNEL_IDまたはRECIPE_CHANNEL_IDが設定されていません。ローカルワーカーを起動できません。")
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