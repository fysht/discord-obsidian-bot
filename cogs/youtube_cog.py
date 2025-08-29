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
import aiohttp
import google.generativeai as genai
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
YOUTUBE_URL_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})')
# リアクションとして検知する絵文字
TRIGGER_EMOJI = '📥'

class YouTubeCog(commands.Cog):
    """YouTube動画の要約とObsidianへの保存を行うCog（ローカル処理担当）"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.youtube_summary_channel_id = int(os.getenv("YOUTUBE_SUMMARY_CHANNEL_ID", 0))

        if not self.gemini_api_key:
            logging.warning("YouTubeCog: GEMINI_API_KEYが設定されていません。")
        else:
            genai.configure(api_key=self.gemini_api_key)
        
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        await self.session.close()

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """特定のリアクションが付与された際に動画要約処理を開始するイベントリスナー"""
        if payload.channel_id != self.youtube_summary_channel_id:
            return
        if payload.user_id == self.bot.user.id:
            return
        if str(payload.emoji) != TRIGGER_EMOJI:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel:
            return
        
        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            logging.warning(f"メッセージの取得に失敗しました: {payload.message_id}")
            return

        is_processed = any(r.emoji in ('✅', '❌', '⏳') and r.me for r in message.reactions)
        if is_processed:
            logging.info(f"既に処理済みのメッセージのためスキップします: {message.jump_url}")
            return

        logging.info(f"リアクション '{TRIGGER_EMOJI}' を検知しました。要約処理を開始します: {message.jump_url}")
        
        try:
            user = self.bot.get_user(payload.user_id) or await self.bot.fetch_user(payload.user_id)
            await message.remove_reaction(payload.emoji, user)
        except (discord.Forbidden, discord.NotFound):
            logging.warning(f"ユーザーリアクションの削除に失敗しました: {message.jump_url}")

        await self._perform_summary(url=message.content.strip(), message=message)

    def _extract_transcript_text(self, fetched_data):
        texts = []
        try:
            for snippet in fetched_data:
                if isinstance(snippet, dict):
                    texts.append(snippet.get('text', ''))
                elif hasattr(snippet, 'text'):
                    texts.append(getattr(snippet, 'text', ''))
                else:
                    texts.append(str(snippet))
            return " ".join(t.strip() for t in texts if t and t.strip())
        except TypeError:
            if isinstance(fetched_data, list):
                for item in fetched_data:
                        if isinstance(item, dict):
                            texts.append(item.get('text', ''))
                return " ".join(t.strip() for t in texts if t and t.strip())
        
        logging.warning(f"予期せぬ字幕データ形式のため、テキスト抽出に失敗しました: {type(fetched_data)}")
        return ""

    async def process_pending_summaries(self):
        """起動時などに未処理の要約リクエストをまとめて処理する関数"""
        channel = self.bot.get_channel(self.youtube_summary_channel_id)
        if not channel:
            logging.error(f"YouTubeCog: チャンネルID {self.youtube_summary_channel_id} が見つかりません。")
            return

        logging.info(f"チャンネル '{channel.name}' の未処理YouTube要約をスキャンします...")
        
        pending_messages = []
        async for message in channel.history(limit=200):
            # TRIGGER_EMOJI（📥）で判定するように修正
            has_pending_reaction = any(r.emoji == TRIGGER_EMOJI for r in message.reactions)
            if has_pending_reaction:
                is_processed = any(r.emoji in ('✅', '❌', '⏳') and r.me for r in message.reactions)
                if not is_processed:
                    pending_messages.append(message)
        
        if not pending_messages:
            logging.info("処理対象の新しいYouTube要約はありませんでした。")
            return

        logging.info(f"{len(pending_messages)}件の未処理YouTube要約が見つかりました。古いものから順に処理します...")
        for message in reversed(pending_messages):
            logging.info(f"処理開始: {message.jump_url}")
            url = message.content.strip()

            try:
                # ユーザーが付けたリアクションをBotが消す場合は .me ではなく、リアクションしたユーザーオブジェクトが必要
                # ここではシンプルにBotがリアクションをクリアしようと試みる
                await message.clear_reaction(TRIGGER_EMOJI)
            except (discord.Forbidden, discord.NotFound):
                logging.warning(f"リアクションの削除に失敗しました: {message.jump_url}")
            
            await self._perform_summary(url=url, message=message)
            await asyncio.sleep(5) # 連続処理のための待機

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

            try:
                fetched = await asyncio.to_thread(
                    YouTubeTranscriptApi().fetch,
                    video_id,
                    languages=['ja', 'en']
                )
            except (TranscriptsDisabled, NoTranscriptFound):
                logging.warning(f"字幕が見つかりませんでした (Video ID: {video_id})")
                if isinstance(message, discord.Message): await message.add_reaction("🔇")
                return
            except Exception as e:
                logging.error(f"字幕取得中に予期せぬエラー (Video ID: {video_id}): {e}", exc_info=True)
                if isinstance(message, discord.Message): await message.add_reaction("❌")
                return
            
            transcript_text = self._extract_transcript_text(fetched)
            if not transcript_text:
                logging.warning(f"字幕テキストが空でした (Video ID: {video_id})")
                if isinstance(message, discord.Message): await message.add_reaction("🔇")
                return
            
            model = genai.GenerativeModel("gemini-2.5-pro")
            
            concise_prompt = (
                "以下のYouTube動画の文字起こし全文を元に、重要なポイントを3～5点で簡潔にまとめてください。\n"
                "要約本文のみを生成し、前置きや返答は一切含めないでください。\n\n"
                f"--- 文字起こし全文 ---\n{transcript_text}"
            )
            
            detail_prompt = (
                "以下のYouTube動画の文字起こし全文を元に、その内容を網羅する詳細で包括的な要約を作成してください。\n"
                "要約本文のみを生成し、前置きや返答は一切含めないでください。\n\n"
                f"--- 文字起こし全文 ---\n{transcript_text}"
            )
            
            tasks = [
                model.generate_content_async(concise_prompt),
                model.generate_content_async(detail_prompt)
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

            concise_summary = responses[0].text if not isinstance(responses[0], Exception) and hasattr(responses[0], 'text') else f"Concise summary generation failed: {responses[0]}"
            detail_summary = responses[1].text if not isinstance(responses[1], Exception) and hasattr(responses[1], 'text') else f"Detailed summary generation failed: {responses[1]}"

            now = datetime.datetime.now(JST)
            daily_note_date = now.strftime('%Y-%m-%d')
            timestamp = now.strftime('%Y%m%d%H%M%S')

            video_info = await self.get_video_info(video_id)
            safe_title = re.sub(r'[\\/*?:"<>|]', "", video_info.get("title", "No Title"))
            
            note_filename = f"{timestamp}-{safe_title}.md"
            note_filename_for_link = note_filename.replace('.md', '')

            note_content = (
                f"# {video_info.get('title', 'No Title')}\n\n"
                f'<iframe width="560" height="315" src="https://www.youtube.com/embed/{video_id}" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>\n\n'
                f"- **URL:** {url}\n"
                f"- **Channel:** {video_info.get('author_name', 'N/A')}\n"
                f"- **作成日:** {daily_note_date}\n\n"
                f"[[{daily_note_date}]]\n\n"
                f"---\n\n"
                f"## Concise Summary\n{concise_summary}\n\n"
                f"## Detailed Summary\n{detail_summary}\n\n"
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
                    else: raise

                link_to_add = f"- [[YouTube/{note_filename_for_link}]]"
                youtube_heading = "\n## 📺 YouTube Summaries"

                if youtube_heading in daily_note_content:
                    daily_note_content = daily_note_content.replace(youtube_heading, f"{youtube_heading}\n{link_to_add}")
                else:
                    daily_note_content += f"\n{youtube_heading}\n{link_to_add}\n"
                
                dbx.files_upload(daily_note_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite'))

            if isinstance(message, discord.Message):
                await message.add_reaction("✅")
            logging.info(f"処理完了: {message.jump_url}")

        except Exception as e:
            logging.error(f"YouTube要約処理全体でエラー: {e}", exc_info=True)
            if isinstance(message, discord.Message): 
                await message.add_reaction("❌")
            elif isinstance(message, discord.InteractionMessage):
                interaction = getattr(message, 'interaction', None)
                if interaction and not interaction.is_response_done():
                    await interaction.followup.send(content=f"❌ 要約処理中にエラーが発生しました: `{e}`", ephemeral=True)

        finally:
            if isinstance(message, discord.Message):
                try:
                    await message.remove_reaction("⏳", self.bot.user)
                except (discord.NotFound, discord.Forbidden):
                    pass

    @app_commands.command(name="yt_summary", description="[手動] YouTube動画のURLを要約してObsidianに保存します。")
    @app_commands.describe(url="要約したいYouTube動画のURL")
    async def yt_summary(self, interaction: discord.Interaction, url: str):
        if not self.gemini_api_key:
            await interaction.response.send_message("⚠️ Gemini APIキーが設定されていません。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        original_response = await interaction.original_response()
        await self._perform_summary(url=url, message=original_response)
        await interaction.followup.send("✅ YouTubeの要約を作成し、保存しました。", ephemeral=True)

    async def get_video_info(self, video_id: str) -> dict:
        url = f"https://www.youtube.com/oembed?url=http://www.youtube.com/watch?v={video_id}&format=json"
        try:
            async with self.session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return {
                        "title": data.get("title"),
                        "author_name": data.get("author_name"),
                    }
                else:
                    logging.warning(f"oEmbedでの動画情報取得に失敗: Status {response.status}")
        except Exception as e:
            logging.warning(f"oEmbedへのリクエスト中にエラー: {e}")
        return {"title": f"YouTube-{video_id}", "author_name": "N/A"}

async def setup(bot: commands.Bot):
    await bot.add_cog(YouTubeCog(bot))