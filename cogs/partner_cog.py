import discord
from discord.ext import commands
from google import genai
from google.genai import types
import os
import datetime
import logging
import re
import zoneinfo

# Services
from services.drive_service import DriveService
from services.webclip_service import WebClipService

JST = zoneinfo.ZoneInfo("Asia/Tokyo")

class PartnerCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID") or os.getenv("DRIVE_FOLDER_ID")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        # サービスの初期化
        self.drive_service = DriveService(self.drive_folder_id)
        self.webclip_service = WebClipService(self.drive_service, self.gemini_api_key)
        
        self.gemini_client = None
        if self.gemini_api_key:
            self.gemini_client = genai.Client(api_key=self.gemini_api_key)
        
        self.last_interaction = None

    async def _fetch_yesterdays_journal(self):
        # 将来的に DriveService から昨日の日記を取得する処理を実装可能
        return ""

    async def _build_conversation_context(self, channel, limit=50, ignore_msg_id=None):
        """会話履歴を取得"""
        messages = []
        async for msg in channel.history(limit=limit, oldest_first=False):
            if ignore_msg_id and msg.id == ignore_msg_id:
                continue
            
            if msg.content.startswith("/"): continue
            if msg.author.bot and msg.author.id != self.bot.user.id: continue
            
            role = "model" if msg.author.id == self.bot.user.id else "user"
            text = msg.content
            if msg.attachments: text += " [メディア送信]"
            messages.append({'role': role, 'text': text})
        
        return list(reversed(messages))

    async def _generate_reply(self, channel, inputs: list, extra_context="", ignore_msg_id=None):
        if not self.gemini_client: return None
        
        weather_info = "天気情報取得不可"
        stock_info = "株価情報取得不可" 
        yesterday_memory = await self._fetch_yesterdays_journal()
        
        system_prompt = (
            f"あなたはユーザーの知的パートナーAIです。\n"
            f"現在日時: {datetime.datetime.now(JST).strftime('%Y-%m-%d %H:%M')}\n"
            f"天気: {weather_info}\n"
            f"株価: {stock_info}\n"
            f"昨日の記憶: {yesterday_memory}\n"
            f"ユーザーの文脈: {extra_context}\n"
            "返答は簡潔に、親しみを込めて。"
        )

        contents = [types.Content(role="user", parts=[types.Part.from_text(text=system_prompt)])]
        
        recent_msgs = await self._build_conversation_context(channel, limit=30, ignore_msg_id=ignore_msg_id)
        
        for msg in recent_msgs:
            contents.append(types.Content(role=msg['role'], parts=[types.Part.from_text(text=msg['text'])]))
        
        user_parts = []
        for inp in inputs:
            if isinstance(inp, str): user_parts.append(types.Part.from_text(text=inp))
            else: user_parts.append(inp)
        
        if user_parts:
            contents.append(types.Content(role="user", parts=user_parts))
        else:
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text="(きっかけ)")]))

        try:
            response = await self.gemini_client.aio.models.generate_content(
                model='gemini-2.5-pro', 
                contents=contents, 
                config=types.GenerateContentConfig(
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
                )
            )
            return response.text
        except Exception as e:
            logging.error(f"GenAI Error: {e}")
            return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        if message.channel.id != self.channel_id: return

        self.last_interaction = datetime.datetime.now(JST)

        url_match = re.search(r'https?://\S+', message.content)
        input_parts = [message.content]
        extra_ctx = ""

        if url_match:
            url = url_match.group()
            async with message.channel.typing():
                # WebClipServiceに処理を委譲
                result = await self.webclip_service.process_url(url, message.content, message)
                
                if result:
                    extra_ctx = result["summary"]

        # 返信生成
        async with message.channel.typing():
            reply = await self._generate_reply(
                message.channel, 
                input_parts, 
                extra_context=extra_ctx, 
                ignore_msg_id=message.id
            )
            if reply:
                await message.reply(reply)

async def setup(bot):
    await bot.add_cog(PartnerCog(bot))