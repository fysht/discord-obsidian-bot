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
MEMO_CHANNEL_ID = int(os.getenv("MEMO_CHANNEL_ID", "0"))
LAST_PROCESSED_TIMESTAMP_FILE = Path(os.getenv("LAST_PROCESSED_TIMESTAMP_FILE", "/var/data/last_processed_timestamp.txt"))

if not TOKEN:
    logging.critical("DISCORD_BOT_TOKENが設定されていません。")
    exit()

# --- 2. Bot本体のクラス定義 ---
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        """Bot起動時の準備処理"""
        logging.info(f"{self.user} としてログインしました (ID: {self.user.id})")
        
        logging.info("オフライン中の未取得メモがないか確認します...")
        after_timestamp = None
        # 最後に処理したタイムスタンプをファイルから読み込む
        if LAST_PROCESSED_TIMESTAMP_FILE.exists():
            try:
                ts_str = LAST_PROCESSED_TIMESTAMP_FILE.read_text().strip()
                if ts_str:
                    after_timestamp = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                    logging.info(f"最終処理時刻: {ts_str} 以降のメッセージを取得します。")
            except Exception as e:
                logging.warning(f"last_processed_timestamp.txt の解析に失敗しました: {e}")

        try:
            channel = await self.fetch_channel(MEMO_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden):
            channel = None
        
        if channel:
            try:
                # after を使って、指定した時刻以降のメッセージのみを取得
                history = [m async for m in channel.history(limit=None, after=after_timestamp)]
                if history:
                    logging.info(f"{len(history)}件の未取得メモが見つかりました。保存します...")
                    for message in sorted(history, key=lambda m: m.created_at):
                        if not message.author.bot:
                            await add_memo_async(
                                content=message.content,
                                author=f"{message.author} ({message.author.id})",
                                created_at=message.created_at.replace(tzinfo=timezone.utc).isoformat()
                            )
                    logging.info("未取得メモの保存が完了しました。")
                else:
                    logging.info("処理対象の新しいメモはありませんでした。")
            except Exception as e:
                logging.error(f"履歴の取得または処理中にエラーが発生しました: {e}", exc_info=True)
        else:
            logging.error(f"MEMO_CHANNEL_ID: {MEMO_CHANNEL_ID} のチャンネルが見つかりません。")

        logging.info("Cogの読み込みを開始します...")
        for filename in os.listdir('./cogs'):
            if filename.endswith('.py'):
                try:
                    await self.load_extension(f'cogs.{filename[:-3]}')
                    logging.info(f" -> {filename} を読み込みました。")
                except Exception as e:
                    logging.error(f" -> {filename} の読み込みに失敗しました: {e}")
        
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