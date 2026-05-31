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
        """サマリー生成を試行し、質問が出た場合のみマネージャーから声をかける。
        nightly_reflection_task と統合され、明日の天気・予定・MIT 質問も同じメッセージに含める。"""
        try:
            from api.routes import (
                _generate_daily_summary,
                _save_daily_summary_to_obsidian,
                _save_manager_qa_to_obsidian,
            )
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

        # 明日のMITを聞く質問も追加（旧 nightly_reflection_task の役割）
        mit_question = "明日のMIT（最重要タスク）を3つ教えて。例:『1. xxx 2. xxx 3. xxx』のように書いてくれれば自動で登録するよ。"
        if mit_question not in existing_texts:
            await add_daily_question(today_str, mit_question, scope='summary')
            new_q_texts.append(mit_question)

        partner_cog = self.bot.get_cog("PartnerCog")

        # 既存の未確定質問を含めた現在の pending を取得
        pending = await get_questions_by_date(today_str, scope='summary')
        unresolved = [q for q in pending if q["status"] != 'resolved']

        if unresolved:
            # 質問が残っている → Obsidian には未保存。マネージャーから声をかける。
            if new_q_texts:
                # 振り返りの問いかけ本文は会話としてそのままチャットへ。
                # 明日の天気・予定は本文に束ねず、個別の通知カード（既読チェック可能）で送る。
                msg_lines = ["今日もお疲れさま！1日の振り返りをまとめたいから、いくつか確認させて📝"]
                for i, q in enumerate(new_q_texts, 1):
                    msg_lines.append(f"{i}. {q}")
                msg_lines.append("")
                msg_lines.append("下の回答欄に書き込んでくれたらサマリーを仕上げて、明日のMITも登録するね🌙")
                msg_lines.append(f"[QUESTIONS:summary:{today_str}]")
                try:
                    from api.notification_service import save_message_and_notify
                    await save_message_and_notify("assistant", "\n".join(msg_lines), proactive=True)
                except Exception as e:
                    logging.error(f"DailySummaryCog send error: {e}")
                await self._send_tomorrow_cards()
            return

        # 質問なし → そのまま Obsidian に保存
        if summary:
            saved = await _save_daily_summary_to_obsidian(today_str, summary)
            if saved:
                await _save_manager_qa_to_obsidian(today_str)
                await resolve_questions(today_str, scope='summary')
                # AI を通すと [ACTION:...] タグが欠落するため、リンク付き通知は直接送る。
                try:
                    from api.notification_service import save_message_and_notify
                    await save_message_and_notify(
                        "assistant",
                        "今日のデイリーサマリーをまとめてObsidianに保存したよ📅 下のボタンから今日の振り返りを見てね🌙\n[ACTION:open_reflection]",
                        title="📅 デイリーサマリー保存", proactive=True,
                    )
                except Exception as e:
                    logging.error(f"DailySummaryCog notify error: {e}")


    async def _send_tomorrow_cards(self):
        """明日の天気・予定を、本文に束ねず個別の通知カード（既読チェック可能）として送る。
        プッシュはまとめて1発。取得失敗した項目はスキップする。"""
        try:
            partner_cog = self.bot.get_cog("PartnerCog")
            info_service = getattr(partner_cog, "info_service", None) if partner_cog else None
            calendar_service = getattr(partner_cog, "calendar_service", None) if partner_cog else None
            notices = []
            if info_service:
                try:
                    wd = await info_service.get_weather()
                    tomorrow_daily = next(
                        (d for d in (wd.get("daily") or []) if d.get("day") == "明日"), None
                    )
                    if tomorrow_daily:
                        body = (
                            f"{tomorrow_daily.get('weather', '')}\n"
                            f"最高 {tomorrow_daily.get('max_temp','?')}℃ / "
                            f"最低 {tomorrow_daily.get('min_temp','?')}℃"
                        )
                        notices.append({"category": "weather", "title": "☀️ 明日の天気", "body": body})
                except Exception as e:
                    logging.debug(f"tomorrow weather fetch error: {e}")
            if calendar_service:
                try:
                    tomorrow_str = (
                        datetime.datetime.now(JST) + datetime.timedelta(days=1)
                    ).strftime("%Y-%m-%d")
                    sched = await calendar_service.list_events_for_date(tomorrow_str)
                    if sched and sched.strip():
                        notices.append(
                            {"category": "schedule", "title": "📅 明日の予定", "body": sched.strip()}
                        )
                except Exception as e:
                    logging.debug(f"tomorrow calendar fetch error: {e}")

            if notices:
                from api.notification_service import send_notice_batch
                await send_notice_batch(notices, "明日のお知らせ")
        except Exception as e:
            logging.debug(f"_send_tomorrow_cards error: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(DailySummaryCog(bot))
