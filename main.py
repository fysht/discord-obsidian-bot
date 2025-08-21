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
# Render環境ではこのファイルは再起動で消えるが、コード上は存在するものとして扱う
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
        # ★ Botの起動時刻を記録（二重処理を防ぐための鍵）
        self.startup_time = datetime.now(timezone.utc)

    async def setup_hook(self):
        logging.info("===== Bot v1.0 Starting up! =====") # v1.1, v1.2のように毎回変える
        
        logging.info(f"{self.user} としてログインしました (ID: {self.user.id})")

        # --- 起動時バックフィル（オフライン中のメッセージも保存） ---
        logging.info("オフライン中の未取得メモがないか確認します...")
        after_timestamp = None
        
        # 最後に処理した時刻のファイルがあれば読み込む
        if LAST_PROCESSED_TIMESTAMP_FILE.exists():
            try:
                ts_str = LAST_PROCESSED_TIMESTAMP_FILE.read_text().strip()
                if ts_str:
                    after_timestamp = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                    logging.info(f"最終処理時刻: {ts_str} 以降のメッセージを取得します。")
            except Exception as e:
                logging.warning(f"last_processed_timestamp.txt の解析に失敗しました: {e}")

        latest_ts_in_history = None
        for guild in self.guilds:
            for channel in guild.text_channels:
                try:
                    # `before=self.startup_time` を指定することで、
                    # この処理はリアルタイム処理と競合しなくなる
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
                    
                    # 処理した履歴の中で最新の時刻を更新
                    current_latest = max(m.created_at for m in history).replace(tzinfo=timezone.utc)
                    if latest_ts_in_history is None or current_latest > latest_ts_in_history:
                        latest_ts_in_history = current_latest

                except discord.Forbidden:
                    logging.warning(f"チャンネルにアクセスできません: {channel.name}")
                except Exception as e:
                    logging.error(f"バックフィル処理中にエラーが発生しました ({channel.name}): {e}", exc_info=True)

        # 最後に処理した時刻を保存（Renderでは一時的だが、短時間の再起動には有効）
        if latest_ts_in_history:
            try:
                LAST_PROCESSED_TIMESTAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
                LAST_PROCESSED_TIMESTAMP_FILE.write_text(latest_ts_in_history.isoformat())
                logging.info(f"最終処理時刻を更新しました: {latest_ts_in_history.isoformat()}")
            except Exception as e:
                 logging.error(f"最終処理時刻の保存に失敗しました: {e}", exc_info=True)


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