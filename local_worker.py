import os
import discord
from discord.ext import commands
import logging
import asyncio
from dotenv import load_dotenv

# 元のコードから必要なCogをインポートする
# YouTubeCogのコードが `youtube_cog.py` にあると仮定
from youtube_cog import YouTubeCog 

load_dotenv()

# --- .envから設定を読み込む ---
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
YOUTUBE_SUMMARY_CHANNEL_ID = int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0))

# --- ログ設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Botのセットアップ ---
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.reactions = True # リアクションの読み取りに必要

bot = commands.Bot(command_prefix="!local!", intents=intents)

# 元のCogをボットに追加
# これにより、_perform_summaryなどの既存ロジックを再利用できる
async def setup_cogs():
    await bot.add_cog(YouTubeCog(bot))

@bot.event
async def on_ready():
    """
    起動したら一度だけ実行される処理。
    未処理のメッセージをスキャンして処理を実行する。
    """
    logging.info(f'{bot.user}としてログインしました (Local - 処理担当)')
    
    youtube_cog = bot.get_cog('YouTubeCog')
    if not youtube_cog:
        logging.error("YouTubeCogが見つかりません。")
        await bot.close()
        return

    try:
        channel = bot.get_channel(YOUTUBE_SUMMARY_CHANNEL_ID)
        if not channel:
            logging.error(f"チャンネルID: {YOUTUBE_SUMMARY_CHANNEL_ID} が見つかりません。")
            await bot.close()
            return

        logging.info(f"チャンネル '{channel.name}' の未処理メッセージをスキャンします...")
        
        # チャンネルの履歴を遡る (件数は適宜調整)
        async for message in channel.history(limit=200):
            # 📥 リアクションが付いているかチェック
            is_pending = False
            for reaction in message.reactions:
                if reaction.emoji == '📥':
                    is_pending = True
                    break
            
            if is_pending:
                logging.info(f"未処理のURLを発見: {message.jump_url}")
                
                # 処理中にリアクションを変更（任意）
                await message.remove_reaction("📥", bot.user)
                
                # 元の要約処理を実行
                url = message.content.strip()
                # _perform_summaryは内部でリアクション（⏳、✅）を追加・削除してくれる
                await youtube_cog._perform_summary(url=url, message=message)
                
                logging.info("処理完了。次のメッセージをスキャンします...")
                await asyncio.sleep(5) # APIレート制限対策

    except Exception as e:
        logging.error(f"処理中にエラーが発生しました: {e}", exc_info=True)
    finally:
        logging.info("すべてのスキャンが完了しました。ボットをシャットダウンします。")
        await bot.close() # 処理が終わったら自動で終了

async def main():
    await setup_cogs()
    await bot.start(BOT_TOKEN)

if __name__ == "__main__":
    # 処理を実行
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("手動でシャットダウンします。")