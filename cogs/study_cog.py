import os
import datetime
from discord.ext import commands
from config import JST
from utils.obsidian_utils import update_section


class StudyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.drive_service = bot.drive_service

    async def append_study_memo(self, subject: str, memo: str):
        """AIパートナーから呼び出される書記関数"""
        service = self.drive_service.get_service()
        if not service:
            return False

        logs_folder_id = await self.drive_service.find_file(
            service, self.drive_folder_id, "StudyLogs"
        )
        if not logs_folder_id:
            logs_folder_id = await self.drive_service.create_folder(
                service, self.drive_folder_id, "StudyLogs"
            )

        file_name = f"{subject}_ノート.md"
        f_id = await self.drive_service.find_file(service, logs_folder_id, file_name)

        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        # Markdownの引用ブロックにして見やすく保存
        formatted_memo = memo.replace("\n", "\n> ")
        append_text = f"- **{now_str}**\n> {formatted_memo}"

        if not f_id:
            content = f"---\ntitle: {subject} 学習ノート\ndate: {now_str[:10]}\ntags: [study]\n---\n\n# {subject} 学習ノート\n\n## 📝 Learning Log\n\n"
            f_id = await self.drive_service.upload_text(
                service, logs_folder_id, file_name, content
            )
            content_to_update = content
        else:
            content_to_update = await self.drive_service.read_text_file(service, f_id)

        new_content = update_section(
            content_to_update, append_text, "## 📝 Learning Log"
        )
        await self.drive_service.update_text(service, f_id, new_content)
        return True


async def setup(bot: commands.Bot):
    await bot.add_cog(StudyCog(bot))
