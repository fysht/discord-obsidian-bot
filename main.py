import os
import sys
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
from services.gmail_service import GmailService
from services.info_service import InfoService

log_format = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=log_format)
load_dotenv()


def _install_gemini_usage_tracker(client) -> None:
    """genai.Client.aio.models.generate_content をラップしてトークン数を SQLite に記録する。
    既存の全呼び出しを変更せず、レスポンスの usage_metadata から自動的に計上する。"""
    try:
        aio_models = client.aio.models
    except AttributeError:
        return
    if getattr(aio_models, "_usage_tracked", False):
        return

    original = aio_models.generate_content

    async def tracked(*args, **kwargs):
        # ---- 自動格下げ: pro 系を要求していて閾値超過の設定なら flash に差し替える ----
        try:
            from services import cost_meter_service
            should_downgrade = await cost_meter_service.should_downgrade_pro_to_flash()
            requested = kwargs.get("model") or (args[0] if args else "")
            if requested and should_downgrade:
                new_model = cost_meter_service.downgrade_model_if_needed(str(requested), True)
                if new_model != requested:
                    logging.info(f"Cost auto-downgrade: {requested} → {new_model}")
                    if "model" in kwargs:
                        kwargs["model"] = new_model
                    elif args:
                        args = (new_model,) + args[1:]
        except Exception as e:
            logging.debug(f"auto-downgrade check failed: {e}")

        response = await original(*args, **kwargs)
        try:
            from api.database import record_api_usage
            model = kwargs.get("model") or (args[0] if args else "") or ""
            meta = getattr(response, "usage_metadata", None)
            in_tokens = getattr(meta, "prompt_token_count", 0) or 0
            out_tokens = getattr(meta, "candidates_token_count", 0) or 0
            if not in_tokens and not out_tokens:
                total = getattr(meta, "total_token_count", 0) or 0
                if total:
                    out_tokens = total
            await record_api_usage(str(model), int(in_tokens), int(out_tokens), source="generate_content")
        except Exception as e:
            logging.debug(f"usage record failed: {e}")
        return response

    aio_models.generate_content = tracked
    aio_models._usage_tracked = True
    logging.info("Geminiトークン計測+自動格下げフックを有効化しました")


def _validate_required_env() -> None:
    """起動時に必須環境変数の存在を検証する。欠落していれば終了。"""
    required = ["PWA_API_KEY", "PWA_PASSWORD"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        logging.error(
            "必須環境変数が未設定のため起動できません: %s",
            ", ".join(missing),
        )
        logging.error(
            ".env もしくはホスティング環境の設定を確認してください。"
            " 詳細は .env.example を参照。"
        )
        sys.exit(1)


_validate_required_env()


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
            self.gmail_service = GmailService(self.drive_service.creds)
        else:
            self.calendar_service = None
            self.tasks_service = None
            self.gmail_service = None
            logging.warning("Google認証情報がありません。Calendar/Tasks/Gmailは無効です。")

        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            self.gemini_client = genai.Client(api_key=api_key)
            try:
                _install_gemini_usage_tracker(self.gemini_client)
            except Exception as e:
                logging.warning(f"Geminiトークン計測の組み込みに失敗: {e}")
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
    # _ready をCogロード前に初期化し、ロード後に set() するだけで十分
    # （全タスクは __init__ で start() し、before_loop で wait_until_ready() を待つ）
    bot._ready = asyncio.Event()
    await bot.setup_hook()
    bot._ready.set()

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
