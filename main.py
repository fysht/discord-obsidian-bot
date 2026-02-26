import os
import asyncio
import logging
from pathlib import Path
import discord
from discord.ext import commands
from dotenv import load_dotenv
import re

from google import genai
from services.drive_service import DriveService
from services.calendar_service import CalendarService

try:
    from obsidian_handler import add_memo_async
except ImportError:
    logging.error("obsidian_handler.pyが見つからないため、起動時メモ処理が無効になります。")
    add_memo_async = None

# --- 1. 設定読み込み ---
log_format = '%(asctime)s [%(levelname)s] [%(name)s] %(message)s'
logging.basicConfig(level=logging.INFO, format=log_format)
load_dotenv()

# --- トークン復元処理 (Render対応) ---
def restore_token_from_env():
    """
    Renderの環境変数 GOOGLE_TOKEN_JSON から token.json を復元します。
    """
    token_json = os.getenv("GOOGLE_TOKEN_JSON")
    token_path = "token.json"
    
    # ファイルがまだなく、環境変数がある場合のみ作成
    if not os.path.exists(token_path) and token_json:
        try:
            logging.info("環境変数 GOOGLE_TOKEN_JSON から token.json を復元します...")
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(token_json)
        except Exception as e:
            logging.error(f"token.json の復元に失敗しました: {e}")

# Bot起動前にトークンを復元
restore_token_from_env()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
MEMO_CHANNEL_ID = int(os.getenv("MEMO_CHANNEL_ID", "0")) 
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

# --- 2. Bot本体のクラス定義 ---
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True 
        intents.members = True       
        intents.reactions = True     
        super().__init__(command_prefix="!", intents=intents) 
        
        # --- DriveServiceの生成 ---
        if GOOGLE_DRIVE_FOLDER_ID:
            self.drive_service = DriveService(GOOGLE_DRIVE_FOLDER_ID)
        else:
            self.drive_service = None
            logging.warning("GOOGLE_DRIVE_FOLDER_IDが設定されていません。")
        
        # --- 追加：CalendarServiceの生成 ---
        if self.drive_service and self.drive_service.creds:
            calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
            self.calendar_service = CalendarService(self.drive_service.creds, calendar_id)
        else:
            self.calendar_service = None
            logging.warning("カレンダー用の認証情報がありません。")
            
        # --- Geminiクライアントの生成 ---
        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            self.gemini_client = genai.Client(api_key=api_key)
        else:
            self.gemini_client = None
            logging.warning("GEMINI_API_KEYが設定されていません。")

    async def setup_hook(self):
        """Cogを動的にロードする"""
        logging.info("Cogの読み込みを開始します...")
        cogs_dir = Path(__file__).parent / 'cogs'

        successful_loads = 0
        failed_loads = []

        # cogsフォルダ内のファイルを自動で読み込む
        for filename in os.listdir(cogs_dir):
            if filename == "__pycache__":
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
        
        if add_memo_async and GOOGLE_DRIVE_FOLDER_ID:
             await self.process_offline_memos()

    async def process_offline_memos(self):
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