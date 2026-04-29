import os
import datetime

from discord.ext import commands

from config import JST
from utils.obsidian_utils import update_section


class BookCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.drive_service = bot.drive_service

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
