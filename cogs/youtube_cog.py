import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import asyncio
import datetime
import zoneinfo
import aiohttp
import google.generativeai as genai
from youtube_transcript_api import YouTubeTranscriptApi

from utils.drive_utils import save_text_to_drive, read_text_from_drive
try:
    from utils.obsidian_utils import update_section
except ImportError:
    def update_section(c, t, h): return f"{c}\n\n{h}\n{t}"

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})')

class YouTubeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.channel_id = int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0))
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        self.gemini = genai.GenerativeModel("gemini-2.5-pro")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.channel_id != self.channel_id or str(payload.emoji) != '📥' or payload.user_id == self.bot.user.id: return
        channel = self.bot.get_channel(payload.channel_id)
        msg = await channel.fetch_message(payload.message_id)
        if any(r.me and str(r.emoji) in '✅⏳' for r in msg.reactions): return
        
        await msg.remove_reaction('📥', self.bot.user)
        await self._process(msg.content, msg)

    async def _process(self, url, msg):
        try:
            await msg.add_reaction("⏳")
            vid_match = YOUTUBE_URL_REGEX.search(url)
            if not vid_match: return await msg.add_reaction("❓")
            
            # 字幕取得
            try:
                transcript = await asyncio.to_thread(YouTubeTranscriptApi.get_transcript, vid_match.group(1), languages=['ja','en'])
                text = " ".join([t['text'] for t in transcript])
            except:
                return await msg.add_reaction("🔇")

            # Gemini要約
            res = await self.gemini.generate_content_async(f"次の動画内容をMarkdownで見出し付き詳細要約せよ:\n{text[:30000]}")
            summary = res.text

            # 保存
            now = datetime.datetime.now(JST)
            date_str = now.strftime('%Y-%m-%d')
            fname = f"{now.strftime('%Y%m%d%H%M%S')}-YouTube.md"
            
            content = f"# YouTube Summary\nURL: {url}\nDate: [[{date_str}]]\n\n{summary}"
            await save_text_to_drive(fname, content, "YouTube")

            # Daily Note
            daily = await read_text_from_drive(f"{date_str}.md", "DailyNotes") or f"# {date_str}\n"
            new_daily = update_section(daily, f"- [[YouTube/{fname[:-3]}|Video Summary]]", "## YouTube")
            await save_text_to_drive(f"{date_str}.md", new_daily, "DailyNotes")

            await msg.add_reaction("✅")
        except Exception as e:
            logging.error(f"YT Error: {e}")
            await msg.add_reaction("❌")

async def setup(bot):
    await bot.add_cog(YouTubeCog(bot))