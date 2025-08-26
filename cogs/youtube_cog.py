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

    def _extract_transcript_text(self, fetched_data):
        """
        youtube-transcript-apiのfetch結果からテキストを抽出するヘルパー関数
        v1系のオブジェクト形式と古い辞書リスト形式の両方に対応
        """
        texts = []
        # イテラブルなオブジェクトかチェック
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
            # イテラブルでない場合、listかチェック
            if isinstance(fetched_data, list):
                for item in fetched_data:
                     if isinstance(item, dict):
                        texts.append(item.get('text', ''))
                return " ".join(t.strip() for t in texts if t and t.strip())
        
        logging.warning(f"予期せぬ字幕データ形式のため、テキスト抽出に失敗しました: {type(fetched_data)}")
        return ""

    async def process_pending_summaries(self):
        """チャンネル履歴を遡り、未処理の要約リクエストをすべて処理する"""
        channel = self.bot.get_channel(self.youtube_summary_channel_id)
        if not channel:
            logging.error(f"YouTubeCog: チャンネルID {self.youtube_summary_channel_id} が見つかりません。")
            return

        logging.info(f"チャンネル '{channel.name}' の未処理YouTube要約をスキャンします...")
        
        pending_messages = []
        async for message in channel.history(limit=200):
            has_pending_reaction = any(r.emoji == '📥' for r in message.reactions)
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
                await message.clear_reaction('📥')
            except discord.Forbidden:
                logging.warning(f"リアクションの削除権限がありません: {message.jump_url}")
            except Exception as e:
                logging.error(f"リアクション削除中に予期せぬエラー: {e}")
            
            await self._perform_summary(url=url, message=message)
            await asyncio.sleep(5) # APIへの負荷を考慮

    async def _perform_summary(self, url: str, message: discord.Message | discord.InteractionMessage):
        """YouTube要約処理のコアロジック"""
        try:
            if isinstance(message, discord.Message):
                await message.add_reaction("⏳")

            # 1. YouTube動画IDの抽出
            video_id_match = YOUTUBE_URL_REGEX.search(url)
            if not video_id_match:
                if isinstance(message, discord.Message): await message.add_reaction("❓")
                return
            video_id = video_id_match.group(1)

            # 2. 字幕の取得とエラーハンドリング
            try:
                # ご希望の fetch() メソッドを、ボットを停止させない asyncio.to_thread で呼び出す
                fetched = await asyncio.to_thread(
                    YouTubeTranscriptApi().fetch(video_id, languages=['ja', 'en'])
                )
            except (TranscriptsDisabled, NoTranscriptFound):
                logging.warning(f"字幕が見つかりませんでした (Video ID: {video_id})")
                if isinstance(message, discord.Message): await message.add_reaction("🔇")
                return
            except Exception as e:
                logging.error(f"字幕取得中に予期せぬエラー (Video ID: {video_id}): {e}", exc_info=True)
                if isinstance(message, discord.Message): await message.add_reaction("❌")
                return

            # 3. 字幕テキストの整形
            transcript_text = self._extract_transcript_text(fetched)
            if not transcript_text:
                logging.warning(f"字幕テキストが空でした (Video ID: {video_id})")
                if isinstance(message, discord.Message): await message.add_reaction("🔇")
                return

            # 4. Gemini APIによる要約
            model = genai.GenerativeModel("gemini-2.5-pro")
            concise_prompt = f"以下のYouTube動画の文字起こしを、重要なポイントを3〜5点で簡潔にまとめてください。\n\n{transcript_text}"
            detail_prompt = f"以下のYouTube動画の文字起こしを、その内容を網羅するように詳細にまとめてください。\n\n{transcript_text}"
            
            concise_task = model.generate_content_async(concise_prompt)
            detail_task = model.generate_content_async(detail_prompt)
            responses = await asyncio.gather(concise_task, detail_task, return_exceptions=True)

            if isinstance(responses[0], Exception) or isinstance(responses[1], Exception):
                logging.error(f"Gemini APIによる要約生成に失敗: {responses}")
                if isinstance(message, discord.Message): await message.add_reaction("❌")
                return

            concise_summary = responses[0].text if not isinstance(responses[0], Exception) else "要約の生成に失敗しました。"
            detail_summary = responses[1].text if not isinstance(responses[1], Exception) else "詳細な要約の生成に失敗しました。"
            
            # 5. 動画タイトルの取得とファイル保存
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
                f"## 詳細な要約\n{detail_summary}\n\n"
                f"---\n\n"
                f"## 字幕（抜粋）\n{transcript_text[:4000]}..."
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

        except Exception as e:
            logging.error(f"YouTube要約処理全体でエラー: {e}", exc_info=True)
            if isinstance(message, discord.Message): await message.add_reaction("❌")
            elif isinstance(message, discord.InteractionMessage):
                await message.edit(content=f"❌ エラーが発生しました: `{e}`")

        finally:
            if isinstance(message, discord.Message):
                try:
                    await message.remove_reaction("⏳", self.bot.user)
                except discord.NotFound:
                    pass # すでにメッセージが消されている場合など

    @app_commands.command(name="yt_summary", description="[手動] YouTube動画のURLを要約してObsidianに保存します。")
    @app_commands.describe(url="要約したいYouTube動画のURL")
    async def yt_summary(self, interaction: discord.Interaction, url: str):
        if not self.gemini_api_key:
            await interaction.response.send_message("⚠️ Gemini APIキーが設定されていません。", ephemeral=True)
            return

        await interaction.response.send_message("⏳ 手動でYouTubeの要約を作成中です...", ephemeral=False)
        message = await interaction.original_response()
        await self._perform_summary(url=url, message=message)

    async def get_video_info(self, video_id: str) -> dict:
        url = f"https://www.youtube.com/oembed?url=http://www.youtube.com/watch?v={video_id}&format=json"
        try:
            async with self.session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return {"title": data.get("title"), "author_name": data.get("author_name")}
                else:
                    logging.warning(f"oEmbedでの動画情報取得に失敗: Status {response.status}")
        except Exception as e:
            logging.warning(f"oEmbedへのリクエスト中にエラー: {e}")
        return {"title": f"YouTube-{video_id}", "author_name": "N/A"}

async def setup(bot: commands.Bot):
    await bot.add_cog(YouTubeCog(bot))