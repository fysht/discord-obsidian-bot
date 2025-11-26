import os
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone
import discord
from discord.ext import commands
from dotenv import load_dotenv
import re
try:
    from obsidian_handler import add_memo_async
except ImportError:
    logging.error("obsidian_handler.pyが見つかりません。起動時メモ処理が無効になります。")
    add_memo_async = None
import dropbox

# --- 1. 設定読み込み ---
log_format = '%(asctime)s [%(levelname)s] [%(name)s] %(message)s'
logging.basicConfig(level=logging.INFO, format=log_format)
load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
MEMO_CHANNEL_ID = int(os.getenv("MEMO_CHANNEL_ID", "0")) 

# --- Dropbox関連設定 ---
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
DROPBOX_VAULT_PATH = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
LAST_PROCESSED_ID_FILE_PATH = f"{DROPBOX_VAULT_PATH}/.bot/last_processed_id.txt"


# --- 2. Bot本体のクラス定義 ---
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True 
        intents.members = True       
        intents.reactions = True     
        super().__init__(command_prefix="!", intents=intents) 

    async def setup_hook(self):
        """Cogをロードする"""
        logging.info("Cogの読み込みを開始します...")
        cogs_dir = Path(__file__).parent / 'cogs'

        successful_loads = 0
        failed_loads = []

        for filename in os.listdir(cogs_dir):
            if filename == "__pycache__":
                continue
            
            # ★ 修正: YouTubeCog と ReceptionCog はローカルワーカーに任せるためスキップ
            # RecipeCog はここでロードさせるため、スキップリストには含めない
            if filename == 'youtube_cog.py' or filename == 'reception_cog.py':
                logging.info(f" -> cogs/{filename} はローカルワーカーが担当するためスキップします。")
                continue

            if filename.endswith('.py') and not filename.startswith('__'):
                cog_name = f'cogs.{filename[:-3]}'
                
                try:
                    await self.load_extension(cog_name)
                    logging.info(f" -> {cog_name} を読み込みました。")
                    successful_loads += 1
                except Exception as e:
                     logging.error(f" -> {cog_name} の読み込みに失敗しました: {e}", exc_info=True)
                     failed_loads.append(f"{cog_name} ({type(e).__name__})")

        logging.info(f"Cog読み込み完了: {successful_loads}個成功")
        if failed_loads:
             logging.error(f"Cog読み込み失敗: {len(failed_loads)}個 - {', '.join(failed_loads)}")

        try:
            await self.tree.sync()
            logging.info(f"グローバルに {len(self.tree.get_commands())} 個のスラッシュコマンドを同期しました。")
        except Exception as e:
             logging.error(f"スラッシュコマンド同期中に予期せぬエラー: {e}", exc_info=True)

    async def on_ready(self):
        logging.info(f"{self.user} としてログインしました (Render - Main Bot)")
        logging.info("--- Bot is ready and listening for events ---")
        
        # オフラインメモ処理などは必要に応じて維持
        if add_memo_async and all([DROPBOX_REFRESH_TOKEN, DROPBOX_APP_KEY, DROPBOX_APP_SECRET]):
             await self.process_offline_memos()

    async def process_offline_memos(self):
        # (既存の処理をそのまま維持してください)
        pass

# --- 3. 起動処理 ---
async def main():
    if not TOKEN:
         logging.critical("DISCORD_BOT_TOKENが設定されていません。ボットを起動できません。")
         return

    bot = MyBot()
    try:
        await bot.start(TOKEN)
    except Exception as e:
         logging.critical(f"ボットの起動中に致命的なエラーが発生しました: {e}", exc_info=True)

URL_REGEX = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+')

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("プログラムが手動で終了されました。")