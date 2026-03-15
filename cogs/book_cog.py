import os
import discord
from discord import app_commands
from discord.ext import commands
import datetime
import aiohttp
import re
import asyncio

from config import JST
from prompts import PROMPT_BOOK_SUMMARY 

class BookCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.drive_service = bot.drive_service
        self.gemini_client = bot.gemini_client

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.channel.id != self.memo_channel_id:
            return

        text = message.content.strip()

        amazon_pattern = r'(https?://(?:www\.)?(?:amazon\.co\.jp|amzn\.to)[^\s]+)'
        match = re.search(amazon_pattern, text)
        
        if match:
            url = match.group(1)
            await message.add_reaction("📚")
            asyncio.create_task(self.process_book_link(message, url))

    async def process_book_link(self, message: discord.Message, url: str):
        title = "名称未設定の本"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    html = await resp.text()
                    match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
                    if match:
                        title = match.group(1).replace("Amazon.co.jp:", "").replace("Amazon.co.jp :", "").strip()
        except Exception:
            pass 

        safe_title = re.sub(r'[\\/*?:"<>|]', '_', title)[:50]

        service = self.drive_service.get_service()
        if service:
            book_folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "BookNotes")
            if not book_folder_id:
                book_folder_id = await self.drive_service.create_folder(service, self.drive_folder_id, "BookNotes")

            file_name = f"{safe_title}.md"
            f_id = await self.drive_service.find_file(service, book_folder_id, file_name)
            if not f_id:
                now_str = datetime.datetime.now(JST).strftime('%Y-%m-%d')
                content = f"---\ntitle: {safe_title}\ndate: {now_str}\ntags: [book]\n---\n\n# {safe_title}\n\n## 📝 Summary & Learning\n\n\n## 📖 Reading Log\n\n"
                await self.drive_service.upload_text(service, book_folder_id, file_name, content)

        msg = await message.reply(f"📚 『{safe_title}』の読書ノートを作成したよ！\nこのスレッドでメモや感想を書いてね。")
        thread = await msg.create_thread(name=f"📖 {safe_title}", auto_archive_duration=10080)
        await thread.send("ここが読書ルームだよ！気軽にメモしたり、わからないことをAIに質問してね。\nまとめを作りたくなったら 「要約して」と話しかけてね。")

    # ★ 追加: AIツールから直接呼び出せる要約メソッド
    async def perform_summarize(self, book_title: str) -> str:
        file_name = f"{book_title}.md"
        service = self.drive_service.get_service()
        if not service: return "Google Driveに接続できなかったよ💦"
        
        book_folder_id = await self.drive_service.find_file(service, self.drive_folder_id, "BookNotes")
        if not book_folder_id: return "BookNotesフォルダが見つからないみたい。"
        
        f_id = await self.drive_service.find_file(service, book_folder_id, file_name)
        if not f_id: return f"ノート（{file_name}）が見つからないよ。"
        
        content = await self.drive_service.read_text_file(service, f_id)
        if not content: return "ノートの読み込みに失敗したよ。"
        
        log_heading = "## 📖 Reading Log"
        summary_heading = "## 📝 Summary & Learning"
        
        if log_heading not in content or summary_heading not in content:
            return "ノートの形式が正しくないみたい（見出しが見つかりません）。"
            
        parts = content.split(log_heading)
        top_half = parts[0].split(summary_heading)[0] 
        raw_log = parts[1].strip()
        
        if not raw_log: return "まだ読書ログがないみたいだよ！"
        
        prompt = f"{PROMPT_BOOK_SUMMARY}\n\n【読書ログ】\n{raw_log}"
        try:
            response = await self.gemini_client.aio.models.generate_content(model="gemini-2.5-pro", contents=prompt)
            summary_text = response.text.strip()
            new_content = f"{top_half}{summary_heading}\n{summary_text}\n\n\n{log_heading}\n{raw_log}\n"
            await self.drive_service.update_text(service, f_id, new_content)
            return f"✨ 『{book_title}』の読書ノートを綺麗に要約して保存したよ！"
        except Exception as e:
            return f"AIの要約中にエラーが発生したよ💦: {e}"

    @app_commands.command(name="summarize_book", description="現在の読書スレッドのログをAIが整理し、ノートの要約を更新します")
    async def summarize_book(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread) or not interaction.channel.name.startswith("📖 "):
            await interaction.response.send_message("このコマンドは「📖」から始まる読書スレッドの中でのみ実行できるよ！", ephemeral=True)
            return

        await interaction.response.defer()
        book_title = interaction.channel.name[2:].strip()
        result = await self.perform_summarize(book_title)
        await interaction.followup.send(result)

async def setup(bot: commands.Bot):
    await bot.add_cog(BookCog(bot))