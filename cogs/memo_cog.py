# cogs/memo_cog.py

import discord
from discord.ext import commands
import json
from datetime import datetime, timezone

# --- 定数定義 ---
TARGET_CHANNEL_NAME = "memo"  # メモを監視するチャンネル名
PENDING_MEMOS_FILE = "pending_memos.json"

class MemoCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # 自身からのメッセージは無視
        if message.author == self.bot.user:
            return

        # 特定のチャンネルのメッセージのみを処理
        if message.channel.name == TARGET_CHANNEL_NAME:
            print(f"'{TARGET_CHANNEL_NAME}' チャンネルでメッセージを検知しました。ファイルへの書き込みを試みます...")

            new_memo = {
                "content": message.content,
                "author": message.author.name,
                # タイムゾーンをUTCに指定してISO 8601形式で保存
                "created_at": datetime.now(timezone.utc).isoformat()
            }

            try:
                # 1. 既存のメモを読み込む
                try:
                    with open(PENDING_MEMOS_FILE, "r", encoding='utf-8') as f:
                        pending_memos = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError):
                    # ファイルがない、または中身が不正な場合は空のリストから開始
                    pending_memos = []

                # 2. 新しいメモを追加
                pending_memos.append(new_memo)

                # 3. 全てのメモをファイルに書き込む
                with open(PENDING_MEMOS_FILE, "w", encoding='utf-8') as f:
                    json.dump(pending_memos, f, ensure_ascii=False, indent=4)
                
                print(f"ファイル '{PENDING_MEMOS_FILE}' への書き込みが成功しました。")

            except Exception as e:
                # ファイル操作中に何らかのエラーが発生した場合、内容をコンソールに表示
                print(f"【書き込みエラー】'{PENDING_MEMOS_FILE}'への保存中に問題が発生しました。")
                print(f"エラー内容: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(MemoCog(bot))