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
import asyncio

# obsidian_handler を使用
try:
    from obsidian_handler import add_memo_async
except ImportError:
    add_memo_async = None

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
TRIGGER_EMOJI = '📝'
SUPPORTED_AUDIO_TYPES = [
    'audio/mpeg', 'audio/x-m4a', 'audio/ogg', 'audio/wav', 'audio/webm'
]

class VoiceMemoCog(commands.Cog):
    """音声メモをテキスト化し、保存するCog (Google Drive同期はWorkerに委任)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")

        # --- 初期チェック ---
        if not self.memo_channel_id:
            logging.warning("VoiceMemoCog: MEMO_CHANNEL_IDが設定されていません。")
        if not self.openai_api_key:
            logging.warning("VoiceMemoCog: OPENAI_API_KEYが設定されていません。")
        if not self.gemini_api_key:
            logging.warning("VoiceMemoCog: GEMINI_API_KEYが設定されていません。")
        
        self.session = aiohttp.ClientSession()
        if self.openai_api_key:
            self.openai_client = openai.AsyncOpenAI(api_key=self.openai_api_key)
        if self.gemini_api_key:
            genai.configure(api_key=self.gemini_api_key)

    async def cog_unload(self):
        await self.session.close()

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.channel_id != self.memo_channel_id: return
        if str(payload.emoji) != TRIGGER_EMOJI: return
        if payload.user_id == self.bot.user.id: return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel: return
        
        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            return
            
        if not message.attachments or not any(att.content_type in SUPPORTED_AUDIO_TYPES for att in message.attachments):
            return

        await self._process_voice_memo(message, message.attachments[0])

    async def _process_voice_memo(self, message: discord.Message, attachment: discord.Attachment):
        """音声メモの処理フロー"""
        temp_audio_path = None
        try:
            await message.add_reaction("⏳")

            # 1. 音声ダウンロード
            temp_audio_path = Path(f"./temp_{attachment.filename}")
            async with self.session.get(attachment.url) as resp:
                if resp.status == 200:
                    with open(temp_audio_path, 'wb') as f:
                        f.write(await resp.read())
                else:
                    raise Exception(f"Download failed: {resp.status}")

            # 2. Whisperで文字起こし
            with open(temp_audio_path, "rb") as audio_file:
                transcription = await self.openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file)
            transcribed_text = transcription.text

            # 3. Geminiで要約・整形
            model = genai.GenerativeModel("gemini-2.5-pro")
            prompt = (
                "以下の文章は音声メモを文字起こししたものです。内容を理解し、重要なポイントを抽出して、箇条書きのMarkdown形式でまとめてください。\n"
                "箇条書きの本文のみを生成し、前置きや返答は一切含めないでください。\n\n"
                f"---\n\n{transcribed_text}"
            )
            response = await model.generate_content_async(prompt)
            formatted_text = response.text.strip()

            # 4. 保存処理 (obsidian_handler経由)
            # 見出し(日時など)は sync_worker が付与するため、ここでは内容のみを渡す
            # ただし、音声メモであることを明示したい場合は content に含める
            content_to_save = f"(Voice Memo)\n{formatted_text}"

            if add_memo_async:
                await add_memo_async(
                    content=content_to_save,
                    author=message.author.display_name,
                    created_at=message.created_at.isoformat(),
                    message_id=message.id,
                    context="VoiceMemo"
                )
                
                # 結果送信
                embed = discord.Embed(title="🎙️ 音声メモを保存しました", description=formatted_text, color=discord.Color.blue())
                await message.channel.send(embed=embed)
                
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("✅")
            else:
                 raise Exception("obsidian_handler がロードされていません")

        except Exception as e:
            logging.error(f"VoiceMemo Error: {e}", exc_info=True)
            try:
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("❌")
            except: pass
        finally:
            if temp_audio_path and os.path.exists(temp_audio_path):
                os.remove(temp_audio_path)

async def setup(bot: commands.Bot):
    if not all([os.getenv("OPENAI_API_KEY"), os.getenv("GEMINI_API_KEY")]):
        logging.error("VoiceMemoCog: API KEY不足")
        return
    await bot.add_cog(VoiceMemoCog(bot))