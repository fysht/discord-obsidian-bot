"""視聴セッションの回収タイマー（ダラ見対策）。

- 宣言タイマー：宣言した分数を過ぎたら「どうだった？」と回収プッシュを 1 回送る。
- ソフトキャップ：宣言なしの自動記録セッションも、長く開きっぱなしなら軽くナッジ。
- ゾンビ掃除：close を取りこぼした古い active を abandoned に自動クローズする。
"""
import datetime
import logging

from discord.ext import commands, tasks

from config import JST

# 宣言なしセッションへ軽くナッジを出す経過時間（秒）。
_SOFT_CAP_SEC = 45 * 60
# close 取りこぼしとみなして自動クローズする経過時間（秒）。
_ZOMBIE_SEC = 6 * 60 * 60

_APP_LABEL = {"youtube": "YouTube", "net": "ネット", "other": "アプリ"}


class WatchTimerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.watch_timer_loop.start()

    def cog_unload(self):
        self.watch_timer_loop.cancel()

    @staticmethod
    def _elapsed_sec(started_at: str) -> int:
        try:
            start = datetime.datetime.fromisoformat(started_at)
            return max(0, int((datetime.datetime.now(JST) - start).total_seconds()))
        except Exception:
            return 0

    @tasks.loop(minutes=1)
    async def watch_timer_loop(self):
        try:
            from api.database import (
                watch_list_active, watch_finalize, watch_mark_reminded,
            )
            from api.notification_service import send_push
        except Exception as e:
            logging.error(f"WatchTimerCog import error: {e}")
            return

        try:
            actives = await watch_list_active()
        except Exception as e:
            logging.debug(f"WatchTimerCog list error: {e}")
            return

        for s in actives:
            sid = s.get("id")
            elapsed = self._elapsed_sec(s.get("started_at"))
            declared = int(s.get("declared_minutes") or 0)
            reminded = int(s.get("reminded") or 0)
            label = _APP_LABEL.get((s.get("app") or "youtube"), "アプリ")

            # ゾンビ掃除（close 取りこぼし）: 実視聴時間は不明なので duration=0 で閉じる。
            if elapsed >= _ZOMBIE_SEC:
                try:
                    await watch_finalize(sid, 0, status="abandoned")
                except Exception as e:
                    logging.debug(f"WatchTimerCog zombie close error ({sid}): {e}")
                continue

            if reminded:
                continue

            # 宣言タイマーの回収
            if declared > 0 and elapsed >= declared * 60:
                body = "宣言した時間が過ぎたよ。どうだった？切り上げる？"
                try:
                    await send_push(
                        title=f"⏰ {label} {declared}分たったよ",
                        body=body, url=f"/?watchEnd={sid}",
                    )
                    await watch_mark_reminded(sid)
                except Exception as e:
                    logging.debug(f"WatchTimerCog declare reminder error ({sid}): {e}")
                continue

            # 宣言なしセッションのソフトキャップ・ナッジ
            if declared == 0 and elapsed >= _SOFT_CAP_SEC:
                mins = elapsed // 60
                try:
                    await send_push(
                        title=f"📵 {label} もう{mins}分見てるよ",
                        body="そろそろ切り上げる？目的は果たせた？",
                        url=f"/?watchEnd={sid}",
                    )
                    await watch_mark_reminded(sid)
                except Exception as e:
                    logging.debug(f"WatchTimerCog soft cap error ({sid}): {e}")

    @watch_timer_loop.before_loop
    async def before_watch_timer(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(WatchTimerCog(bot))
