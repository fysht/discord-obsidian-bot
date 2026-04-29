import os
import asyncio
import logging
from pathlib import Path
import discord
from discord.ext import commands
from dotenv import load_dotenv

from google import genai

from services.google_drive_service import GoogleDriveService
from services.google_calendar_service import GoogleCalendarService
from services.google_tasks_service import GoogleTasksService
from services.info_service import InfoService

log_format = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=log_format)
load_dotenv()


def restore_token_from_env():
    token_json = os.getenv("GOOGLE_TOKEN_JSON")
    token_path = "token.json"
    if not os.path.exists(token_path) and token_json:
        try:
            logging.info("環境変数 GOOGLE_TOKEN_JSON から token.json を復元します...")
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(token_json)
        except Exception as e:
            logging.error(f"token.json の復元に失敗しました: {e}")


restore_token_from_env()

GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")


class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

        if GOOGLE_DRIVE_FOLDER_ID:
            self.drive_service = GoogleDriveService(GOOGLE_DRIVE_FOLDER_ID)
        else:
            self.drive_service = None
            logging.warning("GOOGLE_DRIVE_FOLDER_IDが設定されていません。")

        if self.drive_service and self.drive_service.creds:
            calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
            self.calendar_service = GoogleCalendarService(
                self.drive_service.creds, calendar_id
            )
            self.tasks_service = GoogleTasksService(self.drive_service.creds)
        else:
            self.calendar_service = None
            self.tasks_service = None
            logging.warning("Google認証情報がありません。Calendar/Tasksは無効です。")

        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            self.gemini_client = genai.Client(api_key=api_key)
        else:
            self.gemini_client = None
            logging.warning("GEMINI_API_KEYが設定されていません。")

        self.info_service = InfoService()

    async def setup_hook(self):
        logging.info("Cogの読み込みを開始します...")
        cogs_dir = Path(__file__).parent / "cogs"

        successful_loads = 0
        failed_loads = []

        for filename in os.listdir(cogs_dir):
            if filename == "__pycache__":
                continue
            if filename.endswith(".py") and not filename.startswith("__"):
                cog_name = f"cogs.{filename[:-3]}"
                try:
                    await self.load_extension(cog_name)
                    logging.info(f" -> {cog_name} を読み込みました。")
                    successful_loads += 1
                except Exception as e:
                    logging.error(
                        f" -> {cog_name} の読み込みに失敗しました: {e}", exc_info=True
                    )
                    failed_loads.append(f"{cog_name} ({type(e).__name__})")

        logging.info(f"Cog読み込み完了: {successful_loads}個成功")
        if failed_loads:
            logging.error(f"Cog読み込み失敗: {len(failed_loads)}個 - {', '.join(failed_loads)}")


async def main():
    bot = MyBot()

    import uvicorn
    from api import app as fastapi_app
    from api.routes import router as api_router
    from api.database import init_db, restore_db_from_drive
    from api.chat_service import ChatService

    fastapi_app.include_router(api_router)

    if bot.drive_service and GOOGLE_DRIVE_FOLDER_ID:
        await restore_db_from_drive(bot.drive_service, GOOGLE_DRIVE_FOLDER_ID)
    await init_db()

    chat_service = ChatService(
        gemini_client=bot.gemini_client,
        drive_service=bot.drive_service,
        calendar_service=bot.calendar_service,
        tasks_service=bot.tasks_service,
    )
    fastapi_app.state.chat_service = chat_service
    fastapi_app.state.bot = bot

    # CogをロードしてスケジュールタスクをDiscordなしで起動する
    await bot.setup_hook()
    # discord.py 2.x では _ready は login() 後に初期化されるため、手動で設定する
    import asyncio as _asyncio
    bot._ready = _asyncio.Event()
    bot._ready.set()          # wait_until_ready() を解決する
    bot.dispatch("ready")     # on_ready リスナーを起動する

    port = int(os.getenv("PORT", 10000))
    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)

    logging.info(f"Manager AI サーバーをポート {port} で起動します...")
    try:
        await server.serve()
    except Exception as e:
        logging.error(f"サーバーの起動中にエラーが発生しました: {e}", exc_info=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("プログラムが手動で終了されました。")
