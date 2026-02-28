import discord
from discord.ext import commands
import os
from datetime import datetime
import asyncio

# --- リファクタリング: 定数のクリーンなインポート ---
from config import JST

ZT_FOLDER_NAME = "00_ZeroSecondThinking"

class ZeroSecondThinking(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        
        # --- リファクタリング: Bot本体のサービスを使い回す ---
        self.gemini_client = bot.gemini_client
        self.drive_service = bot.drive_service

    async def _save_to_drive(self, filename, content):
        if not self.drive_folder_id: return False
        service = self.drive_service.get_service()
        if not service: return False

        zt_folder = await self.drive_service.find_file(service, self.drive_folder_id, ZT_FOLDER_NAME)
        if not zt_folder: 
            zt_folder = await self.drive_service.create_folder(service, self.drive_folder_id, ZT_FOLDER_NAME)
        
        file_id = await self.drive_service.find_file(service, zt_folder, filename)
        
        if file_id:
            current_content = await self.drive_service.read_text_file(service, file_id)
            new_content = current_content + content
            await self.drive_service.update_text(service, file_id, new_content)
        else:
            await self.drive_service.upload_text(service, zt_folder, filename, content)
            
        return True

    async def generate_zt_themes(self, keyword=None):
        if not self.gemini_client: return "API Key Error"
        try:
            user_intent = f"「{keyword}」というキーワードに関連して" if keyword else "今、何を書くべきか迷っている状態に対して、頭の中を整理するために"
            prompt = (
                f"あなたは『ゼロ秒思考』のメモ書きファシリテーターです。\n"
                f"ユーザーは{user_intent}、1分間で書き出すためのメモのタイトル（テーマ）を求めています。\n"
                "ユーザーの思考を深掘りする具体的なタイトルを5つ提案してください。\n\n"
                "**条件:**\n"
                "1. 疑問形を中心にする。\n"
                "2. 抽象的な言葉だけでなく、具体的で少しドキッとするような切り口も含める。\n"
                "3. 箇条書きで出力する。\n"
                "4. 余計な挨拶は省略し、テーマ案だけを出力する。"
            )
            response = await self.gemini_client.aio.models.generate_content(
                model='gemini-2.5-pro',
                contents=prompt
            )
            return response.text.strip()
        except Exception as e:
            print(f"Gemini API Error: {e}")
            return "（AI生成エラー）申し訳ありません。現在テーマを生成できません。"

    @commands.command(name='zt_theme', aliases=['theme'])
    async def suggest_theme(self, ctx, *, text=None):
        async with ctx.typing():
            suggestions = await self.generate_zt_themes(text)
        header = f"💡 **「{text if text else 'おまかせ'}」に関するゼロ秒思考テーマ案**"
        message = f"{header}\n\n{suggestions}\n\n*気になったものを1つ選んで、1分間で書き殴ってみましょう！*"
        await ctx.send(message)

    @commands.command(name='zt')
    async def digital_zt(self, ctx, *, content):
        date_str = datetime.now(JST).strftime('%Y-%m-%d')
        filename = f"{date_str}_ZeroSecondThinking.md"
        entry = f"\n\n## {datetime.now(JST).strftime('%H:%M')} (Digital)\n{content}\n"
        
        success = await self._save_to_drive(filename, entry)
        if success: await ctx.message.add_reaction('✅')
        else: await ctx.send("❌ Google Driveへの保存に失敗しました。")

async def setup(bot):
    await bot.add_cog(ZeroSecondThinking(bot))