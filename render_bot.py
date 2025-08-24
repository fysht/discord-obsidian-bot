import os
import discord
from discord.ext import commands
import re
from dotenv import load_dotenv

load_dotenv()

# --- 定数定義 ---
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})')
YOUTUBE_SUMMARY_CHANNEL_ID = int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0))
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# --- Botのセットアップ ---
intents = discord.Intents.default()
intents.message_content = True # メッセージ内容の読み取りに必要

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f'{bot.user}としてログインしました (Render - 受付係)')
    print('YouTubeのURL投稿を監視します...')

@bot.event
async def on_message(message: discord.Message):
    # ボット自身や監視対象外のチャンネルは無視
    if message.author.bot or message.channel.id != YOUTUBE_SUMMARY_CHANNEL_ID:
        return

    # メッセージにYouTubeのURLが含まれているかチェック
    if YOUTUBE_URL_REGEX.search(message.content):
        print(f"URLを検知: {message.content.strip()}")
        try:
            # 処理待ちのリアクションを付ける
            await message.add_reaction("📥") 
            print("📥 リアクションを付けました。")
        except Exception as e:
            print(f"リアクション付与中にエラー: {e}")

# Renderで実行
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("エラー: DISCORD_BOT_TOKENが設定されていません。")
    else:
        bot.run(BOT_TOKEN)