import asyncio
import datetime
import logging
import random

from discord.ext import commands, tasks

from config import JST


class DailySummaryCog(commands.Cog):
    """毎日 22:00 頃にデイリーサマリーを生成し、必要なら質問をユーザーへ通知する。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.daily_summary_loop.start()

    def cog_unload(self):
        self.daily_summary_loop.cancel()

    @tasks.loop(time=datetime.time(hour=22, minute=0, tzinfo=JST))
    async def daily_summary_loop(self):
        await asyncio.sleep(random.randint(0, 600))
        await self._run()

    @daily_summary_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

    async def _run(self):
        """サマリー生成を試行し、質問が出た場合のみマネージャーから声をかける。"""
        try:
            from api.routes import _generate_daily_summary, _save_daily_summary_to_obsidian
            from api.database import (
                add_daily_question, get_questions_by_date, resolve_questions,
            )
        except Exception as e:
            logging.error(f"DailySummaryCog import error: {e}")
            return

        today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        try:
            result = await _generate_daily_summary(today_str)
        except Exception as e:
            logging.error(f"DailySummaryCog generation error: {e}")
            return

        summary = (result.get("summary") or "").strip()
        questions = result.get("questions") or []

        # 既存の未確定質問と重複しないものだけ DB に追加
        existing = await get_questions_by_date(today_str, scope='summary')
        existing_texts = {q["question"].strip() for q in existing}
        new_q_texts = []
        for q in questions:
            if q.strip() and q.strip() not in existing_texts:
                await add_daily_question(today_str, q.strip(), scope='summary')
                new_q_texts.append(q.strip())

        partner_cog = self.bot.get_cog("PartnerCog")

        # 既存の未確定質問を含めた現在の pending を取得
        pending = await get_questions_by_date(today_str, scope='summary')
        unresolved = [q for q in pending if q["status"] != 'resolved']

        if unresolved:
            # 質問が残っている → Obsidian には未保存。マネージャーから声をかける。
            if partner_cog and new_q_texts:
                msg_lines = ["1日のログをまとめようとしたんだけど、いくつか確認したい点があるよ📝"]
                for i, q in enumerate(new_q_texts, 1):
                    msg_lines.append(f"{i}. {q}")
                msg_lines.append("")
                msg_lines.append("時間あるときアプリの『ログ → 1日のログ』から答えてくれたら、続きを書くね🌙")
                instruction = (
                    "以下の内容をユーザーに優しいタメ口で送信してください。改変せずほぼそのまま送ってください。\n\n"
                    + "\n".join(msg_lines)
                )
                try:
                    await partner_cog.generate_and_send_routine_message("", instruction)
                except Exception as e:
                    logging.error(f"DailySummaryCog send error: {e}")
            return

        # 質問なし → そのまま Obsidian に保存
        if summary:
            saved = await _save_daily_summary_to_obsidian(today_str, summary)
            if saved:
                await resolve_questions(today_str, scope='summary')
                if partner_cog:
                    instruction = (
                        "次の文章をユーザーに優しいタメ口で送信してください。改変せずそのまま送ってください。\n\n"
                        "今日の1日のログをまとめてObsidianに保存したよ📅 アプリの『ログ → 1日のログ』から見れるよ🌙"
                    )
                    try:
                        await partner_cog.generate_and_send_routine_message("", instruction)
                    except Exception as e:
                        logging.error(f"DailySummaryCog notify error: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(DailySummaryCog(bot))
