import discord
from discord.ext import commands
import os
import re
import logging

from services.webclip_service import WebClipService
from prompts import PROMPT_URL_RECEPTION # ★ 追加

URL_REGEX = re.compile(r'https?://[^\s]+')

class ReceptionCog(commands.Cog):
    """
    メモチャンネルのURL投稿を監視し、WebClip/YouTubeの即時処理を行うCog
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.drive_service = bot.drive_service
        
        gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.webclip_service = WebClipService(self.drive_service, gemini_api_key)

        if self.memo_channel_id == 0:
            logging.warning("[ReceptionCog] MEMO_CHANNEL_ID が設定されていません。")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.channel.id != self.memo_channel_id:
            return

        match = URL_REGEX.search(message.content)
        if match:
            url = match.group(0)
            logging.info(f"[ReceptionCog] URLを検知し、処理を開始します: {url}")
            await message.add_reaction('⏳')
            
            try:
                # WebClipServiceにURLを渡して処理
                result = await self.webclip_service.process_url(url, message.content, message)
                await message.remove_reaction('⏳', self.bot.user)
                
                # ★ 追加: 処理結果を受け取り、パートナーに会話させる
                if isinstance(result, dict):
                    partner_cog = self.bot.get_cog("PartnerCog")
                    if partner_cog:
                        prompt_text = PROMPT_URL_RECEPTION.format(url_type=result.get('type', 'Webページ'), title=result.get('title', ''))
                        context_data = f"【保存したURLの情報】\n種類: {result.get('type')}\nタイトル: {result.get('title')}\n保存先: {result.get('folder')}/{result.get('file')}"
                        await partner_cog.generate_and_send_routine_message(context_data, prompt_text)
                
            except Exception as e:
                logging.error(f"[ReceptionCog] 処理エラー: {e}", exc_info=True)
                await message.remove_reaction('⏳', self.bot.user)
                await message.add_reaction('❌')

async def setup(bot: commands.Bot):
    await bot.add_cog(ReceptionCog(bot))