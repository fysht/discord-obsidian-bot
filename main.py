import os
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone
import discord
from discord.ext import commands
from dotenv import load_dotenv

from obsidian_handler import add_memo_async

# --- 1. 設定読み込み ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
LAST_PROCESSED_TIMESTAMP_FILE = Path(os.getenv("LAST_PROCESSED_TIMESTAMP_FILE", "/var/data/last_processed_timestamp.txt"))

if not TOKEN:
    logging.critical("DISCORD_BOT_TOKENが設定されていません。")
    exit(1)

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        # --- 修正点: 起動時刻を記録 ---
        self.startup_time = datetime.now(timezone.utc)

    async def setup_hook(self):
        logging.info(f"{self.user} としてログインしました (ID: {self.user.id})")

        # --- 起動時バックフィル（オフライン中のメッセージも保存） ---
        logging.info("オフライン中の未取得メモがないか確認します...")
        after_timestamp = None
        if LAST_PROCESSED_TIMESTAMP_FILE.exists():
            try:
                ts_str = LAST_PROCESSED_TIMESTAMP_FILE.read_text().strip()
                if ts_str:
                    after_timestamp = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                    logging.info(f"最終処理時刻: {ts_str} 以降のメッセージを取得します。")
            except Exception as e:
                logging.warning(f"last_processed_timestamp.txt の解析に失敗しました: {e}")

        latest_ts = None
        for guild in self.guilds:
            for channel in guild.text_channels:
                try:
                    # --- 修正点: ボット起動前のメッセージのみを取得 ---
                    history = [m async for m in channel.history(limit=None, after=after_timestamp, before=self.startup_time)]
                    if not history:
                        continue

                    logging.info(f"[{channel.name}] {len(history)}件の未取得メモが見つかりました。保存します...")
                    for message in sorted(history, key=lambda m: m.created_at):
                        if message.author.bot:
                            continue
                        await add_memo_async(
                            author=f"{message.author} ({message.author.id})",
                            content=message.content,
                            message_id=str(message.id),
                            created_at=message.created_at.replace(tzinfo=timezone.utc).isoformat()
                        )
                    # 最新の時刻を更新
                    latest_ts = max(
                        latest_ts or datetime.min.replace(tzinfo=timezone.utc),
                        max(m.created_at for m in history).replace(tzinfo=timezone.utc)
                    )

                except discord.Forbidden:
                    logging.warning(f"[memo_cog] Cannot access channel: {channel.name}")
                except Exception as e:
                    logging.error(f"[memo_cog] Error while backfilling {channel.name}: {e}", exc_info=True)

        # 最後に処理した時刻を保存
        if latest_ts:
            LAST_PROCESSED_TIMESTAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
            LAST_PROCESSED_TIMESTAMP_FILE.write_text(latest_ts.isoformat())
            logging.info(f"最終処理時刻を更新しました: {latest_ts.isoformat()}")

        # --- Cog 読み込み ---
        logging.info("Cogの読み込みを開始します...")
        for filename in os.listdir('./cogs'):
            if filename.endswith('.py'):
                try:
                    await self.load_extension(f'cogs.{filename[:-3]}')
                    logging.info(f" -> {filename} を読み込みました。")
                except Exception as e:
                    logging.error(f" -> {filename} の読み込みに失敗しました: {e}")

        # スラッシュコマンド同期
        try:
            synced = await self.tree.sync()
            logging.info(f"{len(synced)}個のスラッシュコマンドを同期しました。")
        except Exception as e:
            logging.error(f"スラッシュコマンドの同期に失敗しました: {e}")

# --- 3. 起動処理 ---
async def main():
    bot = MyBot()
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("プログラムが強制終了されました。")