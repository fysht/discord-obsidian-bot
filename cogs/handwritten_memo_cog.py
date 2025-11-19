# cogs/handwritten_memo_cog.py
import os
import discord
from discord.ext import commands
import logging
import aiohttp
import google.generativeai as genai
from datetime import datetime
import zoneinfo
from pathlib import Path
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
from PIL import Image
import io

# 共通関数をインポート
from utils.obsidian_utils import update_section

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
SUPPORTED_IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp']

class HandwrittenMemoCog(commands.Cog):
    """手書きメモ（画像）をテキスト化し、Obsidianに保存するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # --- 環境変数からの設定読み込み ---
        self.channel_id = int(os.getenv("HANDWRITTEN_MEMO_CHANNEL_ID", 0))
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        # Dropbox設定
        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")

        # --- 初期チェックとクライアント初期化 ---
        self.is_ready = False
        if not self.channel_id:
            logging.warning("HandwrittenMemoCog: HANDWRITTEN_MEMO_CHANNEL_IDが設定されていません。")
            return
        if not self.gemini_api_key:
            logging.warning("HandwrittenMemoCog: GEMINI_API_KEYが設定されていません。")
            return
        if not all([self.dropbox_app_key, self.dropbox_app_secret, self.dropbox_refresh_token]):
            logging.warning("HandwrittenMemoCog: Dropboxの認証情報が不足しています。")
            return

        self.session = aiohttp.ClientSession()
        genai.configure(api_key=self.gemini_api_key)
        self.gemini_model = genai.GenerativeModel("gemini-3-pro-preview")
        self.is_ready = True
        logging.info("✅ HandwrittenMemoCogが正常に初期化されました。")


    async def cog_unload(self):
        """Cogのアンロード時にセッションを閉じる"""
        await self.session.close()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """特定チャンネルへの画像投稿を監視する"""
        if not self.is_ready or message.author.bot or message.channel.id != self.channel_id:
            return
        
        # サポートされている形式の画像が添付されているかチェック
        if message.attachments and any(att.content_type in SUPPORTED_IMAGE_TYPES for att in message.attachments):
            # 最初の画像のみを処理対象とする
            image_attachment = next(att for att in message.attachments if att.content_type in SUPPORTED_IMAGE_TYPES)
            await self._process_handwritten_memo(message, image_attachment)

    async def _process_handwritten_memo(self, message: discord.Message, attachment: discord.Attachment):
        """手書きメモの処理フローを実行する"""
        try:
            await message.add_reaction("⏳")

            # 画像をメモリ上にダウンロード
            async with self.session.get(attachment.url) as resp:
                if resp.status != 200:
                    raise Exception(f"画像ファイルのダウンロードに失敗: Status {resp.status}")
                image_bytes = await resp.read()
            
            img = Image.open(io.BytesIO(image_bytes))

            # Gemini (Vision) APIを呼び出し、OCRと整形を一度に行う
            prompt = [
                "この画像は手書きのメモです。内容を読み取り、箇条書きのMarkdown形式でテキスト化してください。返答には前置きや説明は含めず、箇条書きのテキスト本体のみを生成してください。",
                img,
            ]
            response = await self.gemini_model.generate_content_async(prompt)
            formatted_text = response.text.strip()

            # --- Obsidianへの保存処理 ---
            now = datetime.now(JST)
            daily_note_date = now.strftime('%Y-%m-%d')
            current_time = now.strftime('%H:%M')
            
            # 箇条書きの各行をインデントして整形
            content_lines = formatted_text.split('\n')
            indented_content = "\n".join([f"\t{line.strip()}" for line in content_lines])

            content_to_add = (
                f"- {current_time} (handwritten memo)\n"
                f"{indented_content}"
            )

            # Dropboxクライアントを使ってデイリーノートに追記
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

                section_header = "## Handwritten Memos"
                new_content = update_section(daily_note_content, content_to_add, section_header)
                
                dbx.files_upload(
                    new_content.encode('utf-8'),
                    daily_note_path,
                    mode=dropbox.files.WriteMode('overwrite')
                )

            # Discordへのフィードバック
            await message.reply(f"**手書きメモが追加されました** ({current_time})\n{formatted_text}")
            await message.remove_reaction("⏳", self.bot.user)
            await message.add_reaction("✅")
            logging.info(f"手書きメモの処理が正常に完了しました: {message.jump_url}")

        except Exception as e:
            logging.error(f"手書きメモ処理中にエラーが発生: {e}", exc_info=True)
            try:
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("❌")
                await message.reply(f"エラーが発生しました: {e}")
            except discord.HTTPException:
                pass

async def setup(bot: commands.Bot):
    """CogをBotに追加するためのセットアップ関数"""
    await bot.add_cog(HandwrittenMemoCog(bot))