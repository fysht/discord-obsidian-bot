import os
import logging
import datetime
import asyncio
import random

from discord.ext import commands, tasks

from config import JST
from prompts import PROMPT_WEEKLY_REVIEW


class WeeklyReviewCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.drive_service = bot.drive_service
        self.gemini_client = bot.gemini_client

        self.weekly_review_task.start()

    def cog_unload(self):
        self.weekly_review_task.cancel()

    @tasks.loop(time=datetime.time(hour=21, minute=0, tzinfo=JST))
    async def weekly_review_task(self):
        now = datetime.datetime.now(JST)
        if now.weekday() != 6:
            return

        await asyncio.sleep(random.randint(0, 900))

        service = self.drive_service.get_service()
        if not service:
            return

        daily_folder = await self.drive_service.find_file(
            service, self.drive_folder_id, "DailyNotes"
        )
        if not daily_folder:
            return

        gathered_texts = []
        for i in range(7):
            target_date = now - datetime.timedelta(days=i)
            file_name = f"{target_date.strftime('%Y-%m-%d')}.md"
            f_id = await self.drive_service.find_file(service, daily_folder, file_name)
            if f_id:
                try:
                    content = await self.drive_service.read_text_file(service, f_id)
                    extracted = self._extract_key_sections(content)
                    if extracted.strip():
                        gathered_texts.append(
                            f"=== {target_date.strftime('%Y-%m-%d')} ===\n{extracted}"
                        )
                except Exception as e:
                    logging.error(f"WeeklyReview read error for {file_name}: {e}")

        if not gathered_texts:
            logging.info("WeeklyReview: No data found for the week.")
            return

        combined_text = "\n\n".join(reversed(gathered_texts))
        prompt = f"{PROMPT_WEEKLY_REVIEW}\n\n【過去1週間のデータ】\n{combined_text}"

        try:
            if self.gemini_client:
                response = await self.gemini_client.aio.models.generate_content(
                    model="gemini-2.5-pro",
                    contents=prompt,
                )

                send_msg = f"**【今週の Weekly Review 棚卸しレポート】**\n\n{response.text}"
                try:
                    from api.notification_service import save_message_and_notify as _save_msg
                    await _save_msg("assistant", send_msg)
                except Exception:
                    pass
        except Exception as e:
            logging.error(f"WeeklyReview API Error: {e}")

    def _extract_key_sections(self, content: str) -> str:
        sections_to_extract = [
            "## 🪞 Alter Log",
            "## 💡 Insights & Thoughts",
            "## 📝 Events & Actions",
        ]
        extracted = []
        lines = content.split("\n")
        current_section = None

        for line in lines:
            if line.startswith("## "):
                current_section = line.strip()
                extracted.append(line.strip())
            elif current_section in sections_to_extract:
                if line.strip():
                    extracted.append(line.strip())

        return "\n".join(extracted)


    @weekly_review_task.before_loop
    async def before_weekly_review(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(WeeklyReviewCog(bot))
