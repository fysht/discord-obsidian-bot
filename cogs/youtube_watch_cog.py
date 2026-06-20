"""登録チャンネルの新着動画を拾って 1 日 1 回ダイジェスト通知する。

設計（ダラ見対策 / 登録チャンネル新着通知）:
- 登録チャンネル一覧は 1 日 1 回だけ Data API（subscriptions.list）でリフレッシュ。
- 新着検知は各チャンネルの RSS を 30 分間隔でポーリング（API クォータ消費なし）。
  既存動画は INSERT OR IGNORE で弾くので、新規だけが state='new' で積まれる。
- 通知は朝に 1 通だけ「新着 N 本」ダイジェスト。個別の都度通知はしない（ダラ見誘発を避ける）。
"""
import asyncio
import logging

from discord.ext import commands, tasks


class YouTubeWatchCog(commands.Cog):
    _last_subs_refresh_date = None
    _last_digest_date = None

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.poll_loop.start()
        self.subs_refresh_task.start()
        self.digest_task.start()

    def cog_unload(self):
        self.poll_loop.cancel()
        self.subs_refresh_task.cancel()
        self.digest_task.cancel()

    def _service(self):
        return getattr(self.bot, "youtube_service", None)

    # ==========================================================
    # 登録チャンネル一覧のリフレッシュ（1 日 1 回・API）
    # ==========================================================
    @tasks.loop(minutes=1)
    async def subs_refresh_task(self):
        from services.schedule_resolver import is_due
        due, today = await is_due(
            "youtube_subs_refresh", "05:00", "daily", self._last_subs_refresh_date,
        )
        if not due:
            return
        self._last_subs_refresh_date = today
        await self._refresh_subscriptions()

    async def _refresh_subscriptions(self) -> int:
        yt = self._service()
        if not yt or not getattr(yt, "creds", None):
            return 0
        try:
            subs = await yt.list_subscriptions()
            if not subs:
                return 0
            from api.database import youtube_upsert_channels
            await youtube_upsert_channels(subs)
            logging.info(f"YouTubeWatchCog: refreshed {len(subs)} subscriptions")
            return len(subs)
        except Exception as e:
            logging.error(f"YouTubeWatchCog subs refresh error: {e}")
            return 0

    @subs_refresh_task.before_loop
    async def before_subs(self):
        await self.bot.wait_until_ready()

    # ==========================================================
    # 新着動画のポーリング（RSS・30 分間隔）
    # ==========================================================
    @tasks.loop(minutes=30)
    async def poll_loop(self):
        await self._poll_new_videos()

    async def _poll_new_videos(self) -> int:
        yt = self._service()
        if not yt:
            return 0
        try:
            from api.database import youtube_list_channels, youtube_upsert_video
        except Exception as e:
            logging.error(f"YouTubeWatchCog import error: {e}")
            return 0
        channels = await youtube_list_channels(enabled_only=True)
        if not channels:
            return 0  # まだ登録リフレッシュ前。subs_refresh が走るのを待つ。
        new_count = 0
        for ch in channels:
            cid = ch.get("channel_id")
            if not cid:
                continue
            videos = await yt.fetch_channel_feed(cid)
            for v in videos:
                # チャンネル名は RSS 由来が空なら登録名で補完
                if not v.get("channel_title"):
                    v["channel_title"] = ch.get("title") or ""
                v["channel_id"] = cid
                if await youtube_upsert_video(v):
                    new_count += 1
            # 連続 RSS 取得の負荷をならす
            await asyncio.sleep(0.3)
        if new_count:
            logging.info(f"YouTubeWatchCog: {new_count} new videos")
        return new_count

    @poll_loop.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(30)

    # ==========================================================
    # 1 日 1 回ダイジェスト通知
    # ==========================================================
    @tasks.loop(minutes=1)
    async def digest_task(self):
        from services.schedule_resolver import is_due
        due, today = await is_due(
            "youtube_digest", "07:30", "daily", self._last_digest_date,
        )
        if not due:
            return
        self._last_digest_date = today
        await self._send_digest()

    async def _send_digest(self):
        try:
            from api.database import youtube_list_unnotified_new, youtube_mark_notified
        except Exception as e:
            logging.error(f"YouTubeWatchCog digest import error: {e}")
            return
        videos = await youtube_list_unnotified_new()
        if not videos:
            return
        n = len(videos)
        # チャンネル名を数件だけ添えて「何の新着か」を一目で分かるようにする。
        sample = []
        seen = set()
        for v in videos:
            ct = (v.get("channel_title") or "").strip()
            if ct and ct not in seen:
                seen.add(ct)
                sample.append(ct)
            if len(sample) >= 3:
                break
        tail = "ほか" if len(seen) > len(sample) else ""
        chs = ("（" + " / ".join(sample) + tail + "）") if sample else ""
        try:
            from api.notification_service import save_message_and_notify
            await save_message_and_notify(
                "assistant",
                f"📺 登録チャンネルの新着が {n} 本あるよ{chs}。ダラ見せず「あとで見る」に退避しよ！\n[ACTION:open_youtube]",
                proactive=True, title=f"📺 YouTube新着 {n} 本",
            )
            await youtube_mark_notified([v["id"] for v in videos])
        except Exception as e:
            logging.error(f"YouTubeWatchCog digest send error: {e}")

    @digest_task.before_loop
    async def before_digest(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(YouTubeWatchCog(bot))
