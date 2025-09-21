import os
import discord
from discord.ext import commands
import logging
import aiohttp
import openai
import google.generativeai as genai
from datetime import datetime
import zoneinfo
from pathlib import Path
import dropbox
from dropbox.files import WriteMode

# 共通関数をインポート
from utils.obsidian_utils import update_section

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
TRIGGER_EMOJI = '📝'
SUPPORTED_AUDIO_TYPES = [
    'audio/mpeg', 'audio/x-m4a', 'audio/ogg', 'audio/wav', 'audio/webm'
]

class VoiceMemoCog(commands.Cog):
    """音声メモをテキスト化し、Obsidian (via Dropbox) に保存するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # --- 環境変数からの設定読み込み ---
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        # Dropbox設定
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")

        # --- 初期チェック ---
        if not self.memo_channel_id:
            logging.warning("VoiceMemoCog: MEMO_CHANNEL_IDが設定されていません。")
        if not self.openai_api_key:
            logging.warning("VoiceMemoCog: OPENAI_API_KEYが設定されていません。")
        if not self.gemini_api_key:
            logging.warning("VoiceMemoCog: GEMINI_API_KEYが設定されていません。")
        if not all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            logging.warning("VoiceMemoCog: Dropboxの認証情報が不足しています。")

        # --- APIクライアントの初期化 ---
        self.session = aiohttp.ClientSession()
        if self.openai_api_key:
            self.openai_client = openai.AsyncOpenAI(api_key=self.openai_api_key)
        if self.gemini_api_key:
            genai.configure(api_key=self.gemini_api_key)

    async def cog_unload(self):
        """Cogのアンロード時にセッションを閉じる"""
        await self.session.close()

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """リアクションが追加された際のイベントリスナー"""
        # --- トリガー条件のチェック ---
        if payload.channel_id != self.memo_channel_id:
            return
        if str(payload.emoji) != TRIGGER_EMOJI:
            return
        if payload.user_id == self.bot.user.id:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return
        
        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            logging.error(f"メッセージの取得に失敗: {payload.message_id}")
            return
            
        if not message.attachments or not any(att.content_type in SUPPORTED_AUDIO_TYPES for att in message.attachments):
            return

        await self._process_voice_memo(message, message.attachments[0])

    async def _process_voice_memo(self, message: discord.Message, attachment: discord.Attachment):
        """音声メモの処理フローを実行する"""
        temp_audio_path = None
        try:
            await message.add_reaction("⏳")

            temp_audio_path = Path(f"./temp_{attachment.filename}")
            async with self.session.get(attachment.url) as resp:
                if resp.status == 200:
                    with open(temp_audio_path, 'wb') as f:
                        f.write(await resp.read())
                else:
                    raise Exception(f"音声ファイルのダウンロードに失敗: Status {resp.status}")

            with open(temp_audio_path, "rb") as audio_file:
                transcription = await self.openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file)
            transcribed_text = transcription.text

            model = genai.GenerativeModel("gemini-2.5-pro")
            prompt = (
                "以下の文章は音声メモを文字起こししたものです。内容を理解し、重要なポイントを抽出して、箇条書きのMarkdown形式でまとめてください。\n"
                "箇条書きの本文のみを生成し、前置きや返答は一切含めないでください。\n\n"
                f"---\n\n{transcribed_text}"
            )
            response = await model.generate_content_async(prompt)
            formatted_text = response.text.strip()

            now = datetime.now(JST)
            daily_note_date = now.strftime('%Y-%m-%d')
            current_time = now.strftime('%H:%M')
            
            # 箇条書きの各行をインデントして整形
            content_lines = formatted_text.split('\n')
            indented_content = "\n".join([f"\t{line.strip()}" for line in content_lines])

            # 手入力メモと同様のフォーマットを作成
            content_to_add = (
                f"- {current_time} (voice memo)\n"
                f"{indented_content}"
            )

            with dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key,
                app_secret=self.dropbox_app_secret
            ) as dbx:
                daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{daily_note_date}.md"
                
                try:
                    _, res = dbx.files_download(daily_note_path)
                    daily_note_content = res.content.decode('utf-8')
                except dropbox.exceptions.ApiError as e:
                    if isinstance(e.error, dropbox.files.DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                        daily_note_content = "" # ファイルがなければ新規作成
                    else:
                        raise

                # 共通関数を使って ## Memo セクションに追記する
                section_header = "## Memo"
                new_content = update_section(daily_note_content, content_to_add, section_header)
                
                dbx.files_upload(
                    new_content.encode('utf-8'),
                    daily_note_path,
                    mode=WriteMode('overwrite')
                )

            # Discordへの投稿
            await message.channel.send(f"**音声メモが追加されました** ({current_time})\n{formatted_text}")

            await message.remove_reaction("⏳", self.bot.user)
            await message.add_reaction("✅")
            logging.info(f"音声メモの処理が正常に完了しました: {message.jump_url}")

        except Exception as e:
            logging.error(f"音声メモ処理中にエラーが発生: {e}", exc_info=True)
            try:
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("❌")
            except discord.HTTPException:
                pass
        finally:
            if temp_audio_path and os.path.exists(temp_audio_path):
                os.remove(temp_audio_path)

async def setup(bot: commands.Bot):
    """CogをBotに追加する"""
    if not all([os.getenv("OPENAI_API_KEY"), os.getenv("GEMINI_API_KEY"), os.getenv("DROPBOX_REFRESH_TOKEN")]):
        logging.error("VoiceMemoCog: 必要な環境変数が不足しているため、Cogをロードしません。")
        return
    await bot.add_cog(VoiceMemoCog(bot))