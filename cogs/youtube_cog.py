import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import asyncio
import dropbox
from dropbox.files import WriteMode
from dropbox.exceptions import ApiError
import datetime
import zoneinfo
import google.generativeai as genai
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})')


# クラスメソッドを直接呼び出す
def fetch_transcript_sync(video_id: str):
    """
    youtube-transcript-apiを使って字幕データを取得する同期関数
    """
    return YouTubeTranscriptApi.get_transcript(video_id, languages=['ja', 'en'])


class YouTubeCog(commands.Cog):
    """YouTube動画の文字起こしを取得し、要約してObsidianに保存するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # .envファイルから各種キーを読み込む
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.youtube_summary_channel_id = int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0))

        if not self.gemini_api_key:
            logging.warning("YouTubeCog: GEMINI_API_KEYが.envファイルに設定されていません。")
        else:
            genai.configure(api_key=self.gemini_api_key)

    async def _perform_summary(self, url: str, message: discord.Message | discord.InteractionMessage):
        """YouTube要約処理のコアロジック"""
        try:
            if isinstance(message, discord.Message):
                await message.add_reaction("⏳")

            video_id_match = YOUTUBE_URL_REGEX.search(url)
            if not video_id_match:
                if isinstance(message, discord.Message): await message.add_reaction("❓")
                return
            video_id = video_id_match.group(1)

            # 2. 字幕を取得
            try:
                transcript_list = await asyncio.to_thread(fetch_transcript_sync, video_id)
                transcript_text = " ".join([item['text'] for item in transcript_list])

                if not transcript_text.strip():
                    raise NoTranscriptFound(video_id)
            except (TranscriptsDisabled, NoTranscriptFound):
                logging.warning(f"字幕が見つかりませんでした (Video ID: {video_id})")
                if isinstance(message, discord.Message): await message.add_reaction("🔇")
                return
            except Exception as e:
                logging.error(f"字幕取得中に予期せぬエラー (Video ID: {video_id}): {e}", exc_info=True)
                if isinstance(message, discord.Message): await message.add_reaction("❌")
                return

            # 3. Geminiで要約を生成
            model = genai.GenerativeModel("gemini-2.5-pro")
            concise_prompt = f"以下のYouTube動画の文字起こしを、重要ポイントを3〜5点で簡潔にまとめてください。\n\n{transcript_text}"
            detail_prompt = f"以下のYouTube動画の文字起こしを、その内容を網羅するように詳細にまとめてください。\n\n{transcript_text}"

            concise_task = model.generate_content_async(concise_prompt)
            detail_task = model.generate_content_async(detail_prompt)

            responses = await asyncio.gather(concise_task, detail_task, return_exceptions=True)

            if isinstance(responses[0], Exception) or isinstance(responses[1], Exception):
                logging.error(f"Gemini APIによる要約生成に失敗: {responses}")
                if isinstance(message, discord.Message): await message.add_reaction("❌")
                return

            concise_summary = responses[0].text
            detail_summary = responses[1].text

            # 4. Dropboxに保存
            now = datetime.datetime.now(JST)
            daily_note_date = now.strftime('%Y-%m-%d')
            timestamp = now.strftime('%Y%m%d%H%M%S')

            video_info = await self.get_video_info(video_id)
            safe_title = re.sub(r'[\\/*?:"<>|]', "", video_info.get("title", "No Title"))

            note_filename = f"{timestamp}-{safe_title}.md"
            note_filename_for_link = note_filename.replace('.md', '')

            note_content = (
                f"# {video_info.get('title', 'No Title')}\n\n"
                f"- **URL:** <{url}>\n"
                f"- **Channel:** {video_info.get('author_name', 'N/A')}\n"
                f"- **作成日:** {daily_note_date}\n\n"
                f"[[{daily_note_date}]]\n\n"
                f"---\n\n"
                f"## 簡潔な要約（要点）\n{concise_summary}\n\n"
                f"## 詳細な要約\n{detail_summary}\n"
            )

            with dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key,
                app_secret=self.dropbox_app_secret
            ) as dbx:
                note_path = f"{self.dropbox_vault_path}/YouTube/{note_filename}"
                dbx.files_upload(note_content.encode('utf-8'), note_path, mode=WriteMode('add'))

                daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"
                try:
                    _, res = dbx.files_download(daily_note_path)
                    daily_note_content = res.content.decode('utf-8')
                except ApiError as e:
                    if isinstance(e.error, dropbox.files.DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                        daily_note_content = ""
                    else:
                        raise

                link_to_add = f"- [[{note_filename_for_link}]]"
                youtube_heading = "## YouTube Summaries"

                if youtube_heading in daily_note_content:
                    daily_note_content = daily_note_content.replace(youtube_heading, f"{youtube_heading}\n{link_to_add}")
                elif "## WebClips" in daily_note_content:
                    lines = daily_note_content.split('\n')
                    webclips_end_index = -1
                    in_webclips_section = False
                    for i, line in enumerate(lines):
                        if line.strip() == "## WebClips":
                            in_webclips_section = True
                        elif in_webclips_section and line.strip().startswith('## '):
                            webclips_end_index = i
                            break
                    if webclips_end_index == -1:
                        webclips_end_index = len(lines)
                    new_section = f"\n{youtube_heading}\n{link_to_add}"
                    lines.insert(webclips_end_index, new_section)
                    daily_note_content = "\n".join(lines)
                else:
                    new_section = f"{youtube_heading}\n{link_to_add}\n"
                    daily_note_content = (new_section + "\n" + daily_note_content) if daily_note_content.strip() else new_section

                dbx.files_upload(daily_note_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))

            # 5. Discordに送信
            embed = discord.Embed(
                title=f"YouTube要約",
                description=f"**[{video_info.get('title', 'No Title')}]({url})**",
                color=discord.Color.red()
            )
            embed.add_field(name="要点まとめ", value=concise_summary, inline=False)

            if isinstance(message, discord.InteractionMessage):
                await message.edit(content=None, embed=embed)
            else:
                await message.channel.send(embed=embed)

            if isinstance(message, discord.Message):
                await message.add_reaction("✅")

        except Exception as e:
            logging.error(f"YouTube要約処理全体でエラー: {e}", exc_info=True)
            if isinstance(message, discord.Message):
                await message.add_reaction("❌")
            elif isinstance(message, discord.InteractionMessage):
                await message.edit(content=f"❌ エラーが発生しました: `{e}`")

        finally:
            if isinstance(message, discord.Message):
                try:
                    await message.remove_reaction("⏳", self.bot.user)
                except discord.errors.NotFound:
                    pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.channel.id != self.youtube_summary_channel_id:
            return

        if YOUTUBE_URL_REGEX.search(message.content):
            url = message.content.strip()
            await self._perform_summary(url=url, message=message)

    @app_commands.command(name="yt_summary", description="YouTube動画のURLを要約してObsidianに保存します。")
    @app_commands.describe(url="要約したいYouTube動画のURL")
    async def yt_summary(self, interaction: discord.Interaction, url: str):
        if not self.gemini_api_key:
            await interaction.response.send_message("⚠️ Gemini APIキーが設定されていません。", ephemeral=True)
            return

        await interaction.response.send_message("⏳ YouTubeの要約を作成中です...", ephemeral=False)
        message = await interaction.original_response()
        await self._perform_summary(url=url, message=message)

    async def get_video_info(self, video_id: str) -> dict:
        try:
            if not hasattr(self.bot, 'session'):
                self.bot.session = discord.ClientSession()

            async with self.bot.session.get(f"https://www.youtube.com/oembed?url=http://www.youtube.com/watch?v={video_id}&format=json") as response:
                if response.status == 200:
                    data = await response.json()
                    return {"title": data.get("title"), "author_name": data.get("author_name")}
        except Exception as e:
            logging.warning(f"oEmbedでの動画情報取得に失敗: {e}")
        return {"title": f"YouTube-{video_id}", "author_name": "N/A"}


async def setup(bot: commands.Bot):
    await bot.add_cog(YouTubeCog(bot))