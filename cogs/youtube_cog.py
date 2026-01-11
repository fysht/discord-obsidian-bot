import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import asyncio
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
import datetime
import zoneinfo
import aiohttp
import google.generativeai as genai
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

# --- 共通関数をインポート ---
try:
    from utils.obsidian_utils import update_section
except ImportError:
    logging.warning("YouTubeCog: utils/obsidian_utils.pyが見つかりません。")
    def update_section(content, text, header): return f"{content}\n\n{header}\n{text}"

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})')
TRIGGER_EMOJI = '📥'
SECTION_HEADER = "## YouTube Summaries"

class YouTubeCog(commands.Cog):
    """YouTube動画の要約とObsidianへの保存を行うCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.youtube_summary_channel_id = int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0))

        if self.gemini_api_key:
            genai.configure(api_key=self.gemini_api_key)
            # モデルを統一 (Gemini 1.5 Pro)
            self.gemini_model = genai.GenerativeModel("gemini-2.5-pro")
        else:
            logging.warning("YouTubeCog: GEMINI_API_KEYが設定されていません。")
        
        self.session = aiohttp.ClientSession()
        
        self.dbx = None
        if all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            try:
                self.dbx = dropbox.Dropbox(
                    oauth2_refresh_token=self.dropbox_refresh_token,
                    app_key=self.dropbox_app_key,
                    app_secret=self.dropbox_app_secret
                )
            except Exception as e:
                logging.error(f"YouTubeCog: Dropbox Init Error: {e}")

    async def cog_unload(self):
        await self.session.close()

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """特定のリアクションが付与された際に動画要約処理を開始する"""
        if payload.channel_id != self.youtube_summary_channel_id: return
        if payload.user_id == self.bot.user.id: return
        if str(payload.emoji) != TRIGGER_EMOJI: return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return
        
        try: message = await channel.fetch_message(payload.message_id)
        except: return

        if any(str(r.emoji) in ('✅', '❌', '⏳') and r.me for r in message.reactions): return

        try: await message.remove_reaction(payload.emoji, self.bot.user) # 自分(Bot)のリアクションがあれば消す
        except: pass
        
        # ユーザーのリアクションを消すかどうかは運用次第ですが、ここでは処理開始の合図として残すか、消してもOK
        try: await message.remove_reaction(payload.emoji, discord.Object(id=payload.user_id))
        except: pass

        await self._perform_summary(url=message.content.strip(), message=message)

    def _extract_transcript_text(self, fetched_data):
        texts = []
        try:
            for snippet in fetched_data:
                if isinstance(snippet, dict): texts.append(snippet.get('text', ''))
                elif hasattr(snippet, 'text'): texts.append(getattr(snippet, 'text', ''))
                else: texts.append(str(snippet))
            return " ".join(t.strip() for t in texts if t and t.strip())
        except Exception as e:
            logging.warning(f"Transcript extract error: {e}")
            return ""

    async def _perform_summary(self, url: str, message: discord.Message):
        """YouTube要約処理のコアロジック"""
        try:
            await message.add_reaction("⏳")

            video_id_match = YOUTUBE_URL_REGEX.search(url)
            if not video_id_match:
                await message.add_reaction("❓")
                return
            video_id = video_id_match.group(1)

            # 1. 字幕取得
            try:
                fetched = await asyncio.to_thread(
                    YouTubeTranscriptApi().fetch, video_id, languages=['ja', 'en']
                )
            except (TranscriptsDisabled, NoTranscriptFound):
                await message.add_reaction("🔇") # 字幕なし
                return
            except Exception as e:
                logging.error(f"字幕取得エラー: {e}")
                await message.add_reaction("❌")
                return
            
            transcript_text = self._extract_transcript_text(fetched)
            if not transcript_text:
                await message.add_reaction("🔇")
                return
            
            # 2. AI要約
            concise_prompt = f"""
            以下のYouTube動画の文字起こしを元に、重要なポイントを3～5点の箇条書きで簡潔にまとめてください。
            Markdown形式で出力し、前置きは不要です。
            
            --- 文字起こし ---
            {transcript_text[:30000]} 
            """
            # ※文字数が多すぎる場合はカットするなどの処理が必要だが、Gemini 1.5 Proなら長文もいける

            detail_prompt = f"""
            以下のYouTube動画の文字起こしを元に、内容を網羅した詳細な要約記事を作成してください。
            見出しを使って構造化し、読みやすくしてください。
            
            --- 文字起こし ---
            {transcript_text[:30000]}
            """
            
            tasks = [
                self.gemini_model.generate_content_async(concise_prompt),
                self.gemini_model.generate_content_async(detail_prompt)
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

            concise_summary = responses[0].text if not isinstance(responses[0], Exception) else "生成失敗"
            detail_summary = responses[1].text if not isinstance(responses[1], Exception) else "生成失敗"

            # 3. Obsidian保存
            now = datetime.datetime.now(JST)
            date_str = now.strftime('%Y-%m-%d')
            timestamp = now.strftime('%Y%m%d%H%M%S')

            video_info = await self.get_video_info(video_id)
            safe_title = re.sub(r'[\\/*?:"<>|]', "", video_info.get("title", "No Title"))
            
            note_filename = f"{timestamp}-{safe_title}.md"
            note_filename_for_link = note_filename.replace('.md', '')

            # 個別ノートの内容
            note_content = (
                f"# {video_info.get('title', 'No Title')}\n\n"
                f'<iframe width="560" height="315" src="https://www.youtube.com/embed/{video_id}" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>\n\n'
                f"- **URL:** {url}\n"
                f"- **Channel:** {video_info.get('author_name', 'N/A')}\n"
                f"- **Date:** [[{date_str}]]\n\n"
                f"---\n\n"
                f"## Summary (Concise)\n{concise_summary}\n\n"
                f"## Summary (Detail)\n{detail_summary}\n\n"
            )

            if self.dbx:
                # 個別ファイル保存
                note_path = f"{self.dropbox_vault_path}/YouTube/{note_filename}"
                await asyncio.to_thread(self.dbx.files_upload, note_content.encode('utf-8'), note_path, mode=WriteMode('add'))
                
                # デイリーノート更新
                daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
                try:
                    _, res = await asyncio.to_thread(self.dbx.files_download, daily_note_path)
                    daily_content = res.content.decode('utf-8')
                except ApiError as e:
                    if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                        daily_content = f"# Daily Note {date_str}\n"
                    else: raise

                # 共通関数でリンク追記
                link_text = f"- [[YouTube/{note_filename_for_link}|{video_info.get('title', 'YouTube Video')}]]"
                new_daily_content = update_section(daily_content, link_text, SECTION_HEADER)
                
                await asyncio.to_thread(self.dbx.files_upload, new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))

            await message.add_reaction("✅")
            logging.info(f"YouTube summary saved: {safe_title}")

        except Exception as e:
            logging.error(f"YouTube process error: {e}", exc_info=True)
            await message.add_reaction("❌")
        finally:
            try: await message.remove_reaction("⏳", self.bot.user)
            except: pass

    @app_commands.command(name="yt_summary", description="YouTube動画を要約します。")
    async def yt_summary(self, interaction: discord.Interaction, url: str):
        await interaction.response.defer(ephemeral=True)
        # 擬似メッセージオブジェクトを作って処理を流用
        class MockMessage:
            def __init__(self, i, u):
                self.id=i.id; self.content=u; self.jump_url="SlashCommand"
                self.reactions=[]; self.channel=i.channel; self.author=i.user
            async def add_reaction(self, e): pass
            async def remove_reaction(self, e, u): pass
        
        await self._perform_summary(url, MockMessage(interaction, url))
        await interaction.followup.send(f"✅ 要約を開始しました: {url}", ephemeral=True)

    async def get_video_info(self, video_id: str) -> dict:
        url = f"https://www.youtube.com/oembed?url=http://www.youtube.com/watch?v={video_id}&format=json"
        try:
            async with self.session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return {"title": data.get("title"), "author_name": data.get("author_name")}
        except: pass
        return {"title": f"YouTube-{video_id}", "author_name": "N/A"}

async def setup(bot: commands.Bot):
    await bot.add_cog(YouTubeCog(bot))