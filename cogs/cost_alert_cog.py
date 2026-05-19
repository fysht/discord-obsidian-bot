"""毎朝 9:00 JST に今月の API コストをチェックし、閾値を超過していたらプッシュ通知を送る。

設計上のポイント:
- 同じ閾値超過状態を毎日通知してもノイズなので、`cost_alert_last_date` を見て
  「閾値を超えた当日」と「超えたまま 7 日経過するごと」だけ通知する。
- 月初に自動リセット（last_date を空文字に更新）。
"""
import asyncio
import datetime
import logging
import random

from discord.ext import commands, tasks

from config import JST


class CostAlertCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cost_alert_loop.start()

    def cog_unload(self):
        self.cost_alert_loop.cancel()

    @tasks.loop(time=datetime.time(hour=9, minute=0, tzinfo=JST))
    async def cost_alert_loop(self):
        from services.schedule_resolver import is_enabled
        if not await is_enabled("cost_alert"):
            return
        await asyncio.sleep(random.randint(0, 120))
        await self._run()

    @cost_alert_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

    async def _run(self):
        try:
            from services import cost_meter_service
            from api.notification_service import send_push
        except Exception as e:
            logging.error(f"CostAlertCog import error: {e}")
            return

        try:
            now = datetime.datetime.now(JST)
            today_str = now.strftime("%Y-%m-%d")

            # 月初は last_alert_date をリセット
            last = await cost_meter_service.get_last_alert_date()
            if last and last[:7] != today_str[:7]:
                await cost_meter_service.set_last_alert_date("")
                last = ""

            used = await cost_meter_service.current_month_jpy()
            threshold = await cost_meter_service.get_monthly_threshold_jpy()
            if used < threshold:
                return

            # 既に通知済みなら 7 日間隔で再通知のみ
            if last:
                try:
                    last_dt = datetime.datetime.strptime(last, "%Y-%m-%d").date()
                    if (now.date() - last_dt).days < 7:
                        return
                except ValueError:
                    pass

            await send_push(
                title="💰 API料金が月額閾値を超えました",
                body=f"今月のAPI概算コストが ¥{used:.0f}（閾値 ¥{threshold:.0f}）を超過。設定から閾値の見直しや自動格下げの有効化を検討して。",
                url="/?openSettings=1",
            )
            await cost_meter_service.set_last_alert_date(today_str)
            logging.info(f"CostAlertCog: 閾値超過通知を送信 (¥{used:.0f} / ¥{threshold:.0f})")
        except Exception as e:
            logging.error(f"CostAlertCog run error: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(CostAlertCog(bot))
