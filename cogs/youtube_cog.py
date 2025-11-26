import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import asyncio
import dropbox
from dropbox.files import WriteMode
import datetime
import zoneinfo
import google.generativeai as genai
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})')
BOT_PROCESS_TRIGGER_REACTION = '📥' 
PROCESS_START_EMOJI = '⏳'
PROCESS_COMPLETE_EMOJI = '✅'
PROCESS_ERROR_EMOJI = '❌'
TRANSCRIPT_NOT_FOUND_EMOJI = '🔇'

# --- 共通関数インポート ---
try:
    from utils.obsidian_utils import update_section
except ImportError:
    def update_section(current_content, text_to_add, section_header):
        return f"{current_content.strip()}\n\n{section_header}\n{text_to_add}\n"

class YouTubeCog(commands.Cog, name="YouTubeCog"): 
    """YouTube動画の要約・保存を行うCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.youtube_summary_channel_id = int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0))
        
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        self.dbx = None
        self.gemini_model = None
        self.is_ready = False

        if not self.gemini_api_key or not self.dropbox_refresh_token or not self.youtube_summary_channel_id:
            logging.error("YouTubeCog: 必須の環境変数が不足しています。")
            return

        try:
            self.dbx = dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret
            )
            genai.configure(api_key=self.gemini_api_key)
            self.gemini_model = genai.GenerativeModel("gemini-3-pro-preview")
            self.is_ready = True
            logging.info("YouTubeCog: Initialized successfully.")
        except Exception as e:
            logging.error(f"YouTubeCog: Initialization failed: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.is_ready or message.author.bot or message.channel.id != self.youtube_summary_channel_id: return
        if YOUTUBE_URL_REGEX.search(message.content):
            try:
                if not any(str(r.emoji) == BOT_PROCESS_TRIGGER_REACTION and r.me for r in message.reactions):
                    await message.add_reaction(BOT_PROCESS_TRIGGER_REACTION)
            except discord.HTTPException: pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.channel_id != self.youtube_summary_channel_id: return
        if str(payload.emoji) != BOT_PROCESS_TRIGGER_REACTION: return
        
        # ★ 修正ポイント: Bot自身のリアクション以外は無視する (Botがトリガーを引くため)
        if payload.user_id != self.bot.user.id: return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return
        try: message = await channel.fetch_message(payload.message_id)
        except: return

        # 既に処理中または完了済みの場合はスキップ
        if any(r.emoji in (PROCESS_START_EMOJI, PROCESS_COMPLETE_EMOJI) and r.me for r in message.reactions):
            return

        logging.info(f"YouTube trigger detected: {message.jump_url}")
        try: await message.remove_reaction(payload.emoji, await self.bot.fetch_user(payload.user_id))
        except: pass

        await self._perform_summary(message.content.strip(), message)

    async def _perform_summary(self, url: str, message: discord.Message):
        try:
            await message.add_reaction(PROCESS_START_EMOJI)
            
            video_id_match = YOUTUBE_URL_REGEX.search(url)
            if not video_id_match:
                return

            video_id = video_id_match.group(1)
            transcript_text = await self._get_transcript(video_id)
            
            if not transcript_text:
                await message.add_reaction(TRANSCRIPT_NOT_FOUND_EMOJI)
                return

            summary = await self._generate_summary(transcript_text)
            
            embed = discord.Embed(title="📺 YouTube要約 (AI)", description=summary[:4000], color=discord.Color.red(), url=url)
            embed.add_field(name="詳細", value="Obsidianのノートを確認してください。", inline=False)
            await message.reply(embed=embed, mention_author=False)

            await self._save_summary_to_obsidian(url, summary, video_id)
            await message.add_reaction(PROCESS_COMPLETE_EMOJI)

        except Exception as e:
            logging.error(f"YouTube Summary Error: {e}", exc_info=True)
            try: await message.add_reaction(PROCESS_ERROR_EMOJI)
            except: pass
        finally:
            try: await message.remove_reaction(PROCESS_START_EMOJI, self.bot.user)
            except: pass

    async def _get_transcript(self, video_id: str):
        try:
            # 字幕取得処理
            transcript = await asyncio.to_thread(YouTubeTranscriptApi.get_transcript, video_id, languages=['ja', 'en'])
            return " ".join([t['text'] for t in transcript])
        except: return None

    async def _generate_summary(self, text: str) -> str:
        prompt = f"以下のYouTube動画の文字起こしを重要なポイントに絞って3〜5点で要約してください。\n\n{text[:15000]}"
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            return response.text.strip()
        except: return "(要約生成失敗)"

    async def _save_summary_to_obsidian(self, url: str, summary: str, video_id: str):
        now = datetime.datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        timestamp = now.strftime('%Y%m%d%H%M%S')
        filename = f"{timestamp}-YouTube_{video_id}.md"
        
        note_content = f"# YouTube Summary {video_id}\n- URL: {url}\n- Date: {now}\n\n## Summary\n{summary}"
        
        # 個別ノート作成
        await asyncio.to_thread(
            self.dbx.files_upload, 
            note_content.encode('utf-8'), 
            f"{self.dropbox_vault_path}/YouTube/{filename}", 
            mode=WriteMode('add')
        )
        
        # デイリーノート追記
        daily_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        try:
            _, res = await asyncio.to_thread(self.dbx.files_download, daily_path)
            content = res.content.decode('utf-8')
        except: content = f"# {date_str}\n"
        
        link_text = f"- [[YouTube/{filename}|YouTube Summary]]"
        new_content = update_section(content, link_text, "## YouTube Summaries")
        
        await asyncio.to_thread(
            self.dbx.files_upload, 
            new_content.encode('utf-8'), 
            daily_path, 
            mode=WriteMode('overwrite')
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(YouTubeCog(bot))