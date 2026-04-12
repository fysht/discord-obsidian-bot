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
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.drive_service = bot.drive_service
        self.gemini_client = bot.gemini_client

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.weekly_review_task.is_running():
            self.weekly_review_task.start()

    def cog_unload(self):
        self.weekly_review_task.cancel()

    @tasks.loop(time=datetime.time(hour=21, minute=0, tzinfo=JST))
    async def weekly_review_task(self):
        # 実行曜日を日曜日に限定
        now = datetime.datetime.now(JST)
        if now.weekday() != 6: # 6 is Sunday
            return

        # 人間らしさのため0〜15分のランダム遅延
        await asyncio.sleep(random.randint(0, 900))

        channel = self.bot.get_channel(self.memo_channel_id)
        if not channel:
            return

        # 過去7日分の要約を収集
        service = self.drive_service.get_service()
        if not service:
            return

        daily_folder = await self.drive_service.find_file(service, self.drive_folder_id, "DailyNotes")
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
                    # 節約のため、特定のセクションのみ抽出
                    extracted = self._extract_key_sections(content)
                    if extracted.strip():
                        gathered_texts.append(f"=== {target_date.strftime('%Y-%m-%d')} ===\n{extracted}")
                except Exception as e:
                    logging.error(f"WeeklyReview read error for {file_name}: {e}")

        if not gathered_texts:
            logging.info("WeeklyReview: No data found for the week.")
            return

        combined_text = "\n\n".join(reversed(gathered_texts)) # 古い順
        prompt = f"{PROMPT_WEEKLY_REVIEW}\n\n【過去1週間のデータ】\n{combined_text}"

        try:
            if self.gemini_client:
                response = await self.gemini_client.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                )
                
                send_msg = f"**【今週の Weekly Review 棚卸しレポート】**\n\n{response.text}"
                try:
                    from api.database import save_message as _save_msg
                    await _save_msg("assistant", send_msg)
                except Exception:
                    pass
        except Exception as e:
            logging.error(f"WeeklyReview API Error: {e}")

    def _extract_key_sections(self, content: str) -> str:
        """必要な抽出セクション: Alter Log, Insights & Thoughts, Events & Actions"""
        sections_to_extract = ["## 🪞 Alter Log", "## 💡 Insights & Thoughts", "## 📝 Events & Actions"]
        extracted = []
        lines = content.split("\n")
        current_section = None
        
        for line in lines:
            if line.startswith("## "):
                current_section = line.strip()
                extracted.append(line.strip()) # 見出しも残す
            elif current_section in sections_to_extract:
                if line.strip():
                    extracted.append(line.strip())
                        
        return "\n".join(extracted)

async def setup(bot: commands.Bot):
    await bot.add_cog(WeeklyReviewCog(bot))
