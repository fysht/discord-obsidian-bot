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
LAST_PROCESSED_ID_FILE = Path(os.getenv("LAST_PROCESSED_ID_FILE", "/var/data/last_processed_id.txt"))

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
        after_message = None
        if LAST_PROCESSED_ID_FILE.exists():
            try:
                last_id_str = LAST_PROCESSED_ID_FILE.read_text().strip()
                if last_id_str:
                    after_message = discord.Object(id=int(last_id_str))
                    logging.info(f"最終処理ID: {last_id_str} 以降のメッセージを取得します。")
            except Exception as e:
                logging.warning(f"last_processed_id.txt の解析に失敗しました: {e}")

        try:
            channel = await self.fetch_channel(MEMO_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden):
            channel = None
        
        if channel:
            try:
                history = [m async for m in channel.history(limit=None, after=after_message)]
                if history:
                    logging.info(f"{len(history)}件の未取得メモが見つかりました。保存します...")
                    for message in sorted(history, key=lambda m: m.created_at):
                        if not message.author.bot:
                            await add_memo_async(
                                content=message.content,
                                author=f"{message.author} ({message.author.id})",
                                created_at=message.created_at.replace(tzinfo=timezone.utc).isoformat(),
                                message_id=message.id
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