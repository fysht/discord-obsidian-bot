import os
import discord
from discord.ext import commands
import datetime
import re
import asyncio
import aiohttp

from config import JST
from utils.obsidian_utils import update_section


class BookCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.book_channel_id = int(os.getenv("BOOK_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.drive_service = bot.drive_service

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # Amazonリンクからの自動スレッド作成機能のみ残す
        if message.channel.id == self.book_channel_id and not isinstance(
            message.channel, discord.Thread
        ):
            text = message.content.strip()
            amazon_pattern = r"(https?://(?:www\.)?(?:amazon\.co\.jp|amzn\.to)[^\s]+)"
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
                    match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE)
                    if match:
                        title = (
                            match.group(1)
                            .replace("Amazon.co.jp:", "")
                            .replace("Amazon.co.jp :", "")
                            .strip()
                        )
        except Exception:
            pass
        await self._create_book_thread_manual(message, title)

    async def _create_book_thread_manual(self, message: discord.Message, title: str):
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:50]
        service = self.drive_service.get_service()
        if service:
            book_folder_id = await self.drive_service.find_file(
                service, self.drive_folder_id, "BookNotes"
            )
            if not book_folder_id:
                book_folder_id = await self.drive_service.create_folder(
                    service, self.drive_folder_id, "BookNotes"
                )

            file_name = f"{safe_title}.md"
            f_id = await self.drive_service.find_file(
                service, book_folder_id, file_name
            )
            if not f_id:
                now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
                content = f"---\ntitle: {safe_title}\ndate: {now_str}\ntags: [book]\n---\n\n# {safe_title}\n\n## 📖 Reading Log\n\n"
                await self.drive_service.upload_text(
                    service, book_folder_id, file_name, content
                )

        await message.reply(f"📚 『{safe_title}』の読書ノートを作成したよ！")

    async def append_book_memo(self, book_title: str, memo: str):
        """AIパートナーから呼び出される書記関数"""
        service = self.drive_service.get_service()
        if not service:
            return False

        book_folder_id = await self.drive_service.find_file(
            service, self.drive_folder_id, "BookNotes"
        )
        if not book_folder_id:
            book_folder_id = await self.drive_service.create_folder(
                service, self.drive_folder_id, "BookNotes"
            )

        file_name = f"{book_title}.md"
        f_id = await self.drive_service.find_file(service, book_folder_id, file_name)

        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        formatted_memo = memo.replace("\n", "\n> ")
        append_text = f"- **{now_str}** 👤 記録:\n> {formatted_memo}"

        if not f_id:
            content = f"---\ntitle: {book_title}\ndate: {now_str[:10]}\ntags: [book]\n---\n\n# {book_title}\n\n## 📖 Reading Log\n\n"
            await self.drive_service.upload_text(
                service, book_folder_id, file_name, content
            )
            f_id = await self.drive_service.find_file(
                service, book_folder_id, file_name
            )
            content_to_update = content
        else:
            content_to_update = await self.drive_service.read_text_file(service, f_id)

        new_content = update_section(
            content_to_update, append_text, "## 📖 Reading Log"
        )
        await self.drive_service.update_text(service, f_id, new_content)
        return True


async def setup(bot: commands.Bot):
    await bot.add_cog(BookCog(bot))
