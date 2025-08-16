# cogs/memo_cog.py

import discord
from discord.ext import commands
import os
import datetime
import json

LAST_MESSAGE_ID_FILE = "last_message_id.txt"
PENDING_MEMOS_FILE = "pending_memos.json"

class MemoCog(commands.Cog):
    """'memo'チャンネルへの投稿を監視し、ローカルに一時保存するCog"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.last_message_id = self._load_last_message_id()

    def _load_last_message_id(self) -> int | None:
        try:
            with open(LAST_MESSAGE_ID_FILE, "r") as f:
                return int(f.read().strip())
        except (FileNotFoundError, ValueError):
            return None

    def _save_last_message_id(self, message_id: int):
        with open(LAST_MESSAGE_ID_FILE, "w") as f:
            f.write(str(message_id))
        self.last_message_id = message_id

    def _get_pending_memos(self) -> list:
        try:
            with open(PENDING_MEMOS_FILE, "r", encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save_pending_memos(self, memos: list):
        with open(PENDING_MEMOS_FILE, "w", encoding='utf-8') as f:
            json.dump(memos, f, ensure_ascii=False, indent=2)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.channel.id != self.memo_channel_id:
            return

        print(f"'{message.channel.name}' チャンネルでメッセージを検知しました。キューに追加します。")
        
        channel = message.channel
        
        messages_to_queue = []
        async for msg in channel.history(limit=None, after=discord.Object(id=self.last_message_id) if self.last_message_id else None):
            messages_to_queue.append(msg)
        
        if not messages_to_queue:
            return
            
        pending_memos = self._get_pending_memos()
        for msg in messages_to_queue:
            memo_data = {
                "id": msg.id,
                "created_at": msg.created_at.isoformat(),
                "author": msg.author.display_name,
                "content": msg.clean_content
            }
            pending_memos.append(memo_data)

        self._save_pending_memos(pending_memos)
        
        last_msg_id = messages_to_queue[-1].id
        self._save_last_message_id(last_msg_id)
        
        await message.add_reaction("📥")

async def setup(bot: commands.Bot):
    await bot.add_cog(MemoCog(bot))