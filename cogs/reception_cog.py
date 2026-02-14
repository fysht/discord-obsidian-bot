import discord
from discord.ext import commands
import os
import re
import logging

# Servicesの読み込み (フォルダ構成に合わせてインポート)
from services.drive_service import DriveService
from services.webclip_service import WebClipService

# 一般的なURLを抽出する正規表現
URL_REGEX = re.compile(r'https?://[^\s]+')

class ReceptionCog(commands.Cog):
    """
    メモチャンネルのURL投稿を監視し、WebClip/YouTubeの即時処理を行うCog
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        
        # サービス群の初期化
        gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        # 【修正箇所】環境変数からフォルダIDを取得して DriveService に渡す
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID") 
        self.drive_service = DriveService(folder_id) 
        
        self.webclip_service = WebClipService(self.drive_service, gemini_api_key)

        if self.memo_channel_id == 0:
            logging.warning("[ReceptionCog] MEMO_CHANNEL_ID が設定されていません。")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Bot自身のメッセージや、メモチャンネル以外は無視
        if message.author.bot:
            return
        
        if message.channel.id != self.memo_channel_id:
            return

        # メッセージの中にURLが含まれているかチェック
        match = URL_REGEX.search(message.content)
        if match:
            url = match.group(0)
            logging.info(f"[ReceptionCog] URLを検知し、処理を開始します: {url}")
            
            # 処理中のリアクションを付ける
            await message.add_reaction('⏳')
            
            try:
                # WebClipServiceにURLを渡して即時処理（YouTubeかWeb記事かはサービス側で自動判定されます）
                result = await self.webclip_service.process_url(url, message.content, message)
                
                # 処理が終了したら⏳リアクションを外す
                await message.remove_reaction('⏳', self.bot.user)
                
            except Exception as e:
                logging.error(f"[ReceptionCog] URL処理中にエラーが発生しました: {e}", exc_info=True)
                await message.remove_reaction('⏳', self.bot.user)
                await message.add_reaction('❌')

async def setup(bot: commands.Bot):
    await bot.add_cog(ReceptionCog(bot))