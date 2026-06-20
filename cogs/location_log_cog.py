import os
import logging
import tempfile
from datetime import datetime, time, timedelta
import asyncio
import re

import ijson
from discord.ext import commands, tasks
import googlemaps
from geopy.distance import great_circle
from googleapiclient.http import MediaIoBaseDownload

from config import JST
from utils.obsidian_utils import update_section
from prompts import PROMPT_LOCATION_SYNC

DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ACTIVITY_TYPE_MAP = {
    "IN_PASSENGER_VEHICLE": "車での移動",
    "WALKING": "徒歩での移動",
    "CYCLING": "自転車での移動",
    "RUNNING": "ランニング",
    "IN_BUS": "バスでの移動",
    "IN_TRAIN": "電車での移動",
    "IN_SUBWAY": "地下鉄での移動",
    "IN_TRAM": "路面電車での移動",
    "IN_FERRY": "フェリーでの移動",
    "FLYING": "飛行機での移動",
    "STILL": "静止",
    "UNKNOWN": "不明な移動",
}


class LocationLogCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.drive_service = bot.drive_service
        self.home_coordinates = self._parse_coordinates(os.getenv("HOME_COORDINATES"))
        self.work_coordinates = self._parse_coordinates(os.getenv("WORK_COORDINATES"))
        self.exclude_radius_meters = int(os.getenv("EXCLUDE_RADIUS_METERS", 500))
        self.google_places_api_key = os.getenv("GOOGLE_PLACES_API_KEY")
        self.gmaps = (
            googlemaps.Client(key=self.google_places_api_key)
            if self.google_places_api_key
            else None
        )
        # place_id → 地名のキャッシュ。同じ場所（いつもの駅・店など）の
        # 重複した Places API 課金を避ける。
        self._place_name_cache: dict[str, str] = {}

        self.process_timeline_json.start()
        self.location_save_reminder_task.start()

    def cog_unload(self):
        self.process_timeline_json.cancel()
        self.location_save_reminder_task.cancel()

    def _get_place_name_from_id(self, place_id: str) -> str:
        if not self.gmaps:
            return f"場所ID: {place_id}"
        # 同一 place_id はキャッシュから返し、重複した課金を避ける。
        cached = self._place_name_cache.get(place_id)
        if cached is not None:
            return cached
        # fields=["name"] を指定して取得フィールドを名前のみに絞り、課金 SKU を最小化する。
        name = f"場所ID: {place_id}"
        try:
            place_details = self.gmaps.place(
                place_id=place_id, language="ja", fields=["name"]
            )
            if (
                place_details
                and "result" in place_details
                and "name" in place_details["result"]
            ):
                name = place_details["result"]["name"]
        except Exception as e:
            logging.error(f"Places APIからの名前取得に失敗: {e}")
        self._place_name_cache[place_id] = name
        return name

    def _parse_coordinates(self, coord_str: str | None) -> tuple[float, float] | None:
        if not coord_str:
            return None
        try:
            lat, lon = map(float, coord_str.split(","))
            return (lat, lon)
        except (ValueError, TypeError):
            return None

    def _format_duration(self, duration_seconds: float) -> str:
        minutes = int(duration_seconds / 60)
        if minutes < 1:
            return "1分未満"
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours}時間{minutes}分"
        return f"{minutes}分"

    def _parse_iso_timestamp(self, ts_str: str) -> datetime | None:
        try:
            if ts_str.count(":") == 3:
                ts_str = ts_str[::-1].replace(":", "", 1)[::-1]
            return datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            return None

    def _find_folder_in_root(self, service, name):
        query = f"'root' in parents and name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        res = service.files().list(q=query, fields="files(id)").execute()
        files = res.get("files", [])
        return files[0]["id"] if files else None

    def _get_unprocessed_json(self, service, folder_id):
        query = f"'{folder_id}' in parents and name contains 'タイムライン.json' and not name contains '処理済み_' and trashed = false"
        res = service.files().list(q=query, fields="files(id, name)").execute()
        return res.get("files", [])

    def _get_latest_timeline_json(self, service, folder_id):
        query = f"'{folder_id}' in parents and name contains 'タイムライン.json' and trashed = false"
        res = (
            service.files()
            .list(
                q=query,
                fields="files(id, name, createdTime)",
                orderBy="createdTime desc",
            )
            .execute()
        )
        files = res.get("files", [])
        return files[0] if files else None

    def _rename_file(self, service, file_id, new_name):
        service.files().update(fileId=file_id, body={"name": new_name}).execute()

    def _read_and_extract(self, service, file_id, target_dates: set[str] = None) -> dict:
        """Timeline JSON を一時ファイルへストリーム保存し、semanticSegments を ijson で
        1 件ずつ読みながらログ抽出する。巨大なタイムライン JSON でも全体をメモリへ
        展開しないため、Render の 23:50 メモリ超過を防ぐ。"""
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
                tmp_path = tf.name
                downloader = MediaIoBaseDownload(
                    tf, service.files().get_media(fileId=file_id)
                )
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            with open(tmp_path, "rb") as f:
                segments = ijson.items(f, "semanticSegments.item")
                return self._extract_logs_from_json(segments, target_dates=target_dates)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def _extract_logs_from_json(self, segments, target_dates: set[str] = None) -> dict:
        """semanticSegments の iterable（ijson ストリーム or list）からログを抽出する。"""
        events_by_date = {}
        for seg in (segments or []):
            start_time = self._parse_iso_timestamp(seg.get("startTime", ""))
            end_time = self._parse_iso_timestamp(seg.get("endTime", ""))
            if not start_time or not end_time:
                continue

            event_date = start_time.astimezone(JST).date()
            date_str = event_date.strftime("%Y-%m-%d")

            if target_dates and date_str not in target_dates:
                continue

            events_by_date.setdefault(date_str, [])
            duration_seconds = (end_time - start_time).total_seconds()
            duration_formatted = self._format_duration(duration_seconds)
            event = {"start": start_time, "end": end_time}

            if visit_data := seg.get("visit"):
                top_candidate = visit_data.get("topCandidate", {})
                lat_lng_str = top_candidate.get("placeLocation", {}).get("latLng")
                if not lat_lng_str:
                    continue
                try:
                    lat_str, lon_str = lat_lng_str.replace("°", "").split(",")
                    place_coords = (float(lat_str), float(lon_str.strip()))
                except (ValueError, IndexError):
                    continue

                # 自宅・勤務先の判定を Places API 呼び出しより先に行う。
                # 最も頻度の高い自宅/勤務先では有料 API を呼ばず、コストを抑える。
                if (
                    self.home_coordinates
                    and great_circle(place_coords, self.home_coordinates).meters
                    < self.exclude_radius_meters
                ):
                    place_name = "自宅"
                elif (
                    self.work_coordinates
                    and great_circle(place_coords, self.work_coordinates).meters
                    < self.exclude_radius_meters
                ):
                    place_name = "勤務先"
                else:
                    place_name = "不明な場所"
                    place_id = top_candidate.get("placeId")
                    if place_id:
                        place_name = self._get_place_name_from_id(place_id)

                event.update(
                    {"type": "stay", "name": place_name, "duration": duration_formatted}
                )
                events_by_date[date_str].append(event)

            elif activity_data := seg.get("activity"):
                activity_type = activity_data.get("topCandidate", {}).get(
                    "type", "UNKNOWN"
                )
                distance_m = activity_data.get("distanceMeters", 0)
                distance_km_str = (
                    f" (約{distance_m / 1000:.1f}km)" if distance_m > 0 else ""
                )
                event.update(
                    {
                        "type": "move",
                        "activity": ACTIVITY_TYPE_MAP.get(activity_type, "不明な移動"),
                        "duration": duration_formatted,
                        "distance": distance_km_str,
                    }
                )
                events_by_date[date_str].append(event)

        logs_by_date = {}
        for d_str, events in sorted(events_by_date.items()):
            if not events:
                continue
            sorted_events = sorted(events, key=lambda x: x["start"])
            log_entries, last_place = [], None

            for event in sorted_events:
                start_str_jst = event["start"].astimezone(JST).strftime("%H:%M")
                if event["type"] == "stay":
                    if last_place is not None:
                        log_entries.append(
                            f"- **{start_str_jst}** {event['name']}に到着"
                        )
                    log_entries.append(
                        f"- **{start_str_jst} - {event['end'].astimezone(JST).strftime('%H:%M')}** ({event['duration']}) 滞在: {event['name']}"
                    )
                    last_place = event["name"]
                elif event["type"] == "move":
                    if last_place:
                        log_entries.append(f"- **{start_str_jst}** {last_place}を出発")
                    log_entries.append(
                        f"- **{start_str_jst} - {event['end'].astimezone(JST).strftime('%H:%M')}** ({event['duration']}) {event['activity']}{event['distance']}"
                    )
                    last_place = None

            logs_by_date[d_str] = "\n".join(log_entries)

        return logs_by_date

    async def _dates_with_location_history(self, service, dates: set[str]) -> set[str]:
        """指定日のうち、既に DailyNote に Location History が書かれている日を返す。
        Places API（有料）を呼ぶ前のプレフィルタ用。Drive の読み取りは無料。"""
        recorded: set[str] = set()
        try:
            folder_id = await self.drive_service.find_file(
                service, self.drive_folder_id, "DailyNotes"
            )
        except Exception:
            folder_id = None
        if not folder_id:
            return recorded
        for d in dates:
            try:
                f_id = await self.drive_service.find_file(service, folder_id, f"{d}.md")
                if not f_id:
                    continue
                content = await self.drive_service.read_text_file(service, f_id)
                if content and re.search(r"## 📍 Location History\s*-", content):
                    recorded.add(d)
            except Exception:
                continue
        return recorded

    async def _write_to_obsidian(
        self, date_str: str, log_text: str, force: bool = False
    ) -> bool:
        service = self.drive_service.get_service()
        if not service:
            return False

        daily_folder = await self.drive_service.find_file(
            service, self.drive_folder_id, "DailyNotes"
        )
        if not daily_folder:
            daily_folder = await self.drive_service.create_folder(
                service, self.drive_folder_id, "DailyNotes"
            )

        daily_file = await self.drive_service.find_file(
            service, daily_folder, f"{date_str}.md"
        )

        cur = ""
        if daily_file:
            try:
                cur = await self.drive_service.read_text_file(service, daily_file)
            except Exception as e:
                logging.error(f"ファイル読み込みエラー: {e}")

        if not force and re.search(r"## 📍 Location History\s*-", cur):
            return False

        if not cur:
            cur = f"---\ndate: {date_str}\n---\n\n# Daily Note {date_str}\n\n## 📍 Location History\n\n"

        new = update_section(cur, log_text, "## 📍 Location History")

        if daily_file:
            await self.drive_service.update_text(service, daily_file, new)
        else:
            await self.drive_service.upload_text(
                service, daily_folder, f"{date_str}.md", new
            )

        return True

    async def perform_manual_sync(self, target_date: str) -> str:
        if not DATE_REGEX.match(target_date):
            return "❌ 日付の形式が正しくありません。(例: 2026-02-15)"
        loop = asyncio.get_running_loop()
        service = self.drive_service.get_service()
        if not service:
            return "Google Driveに接続できませんでした。"

        timeline_folder_id = await loop.run_in_executor(
            None, self._find_folder_in_root, service, "Timeline"
        )
        if not timeline_folder_id:
            return "Timelineフォルダが見つかりません。"

        latest_file = await loop.run_in_executor(
            None, self._get_latest_timeline_json, service, timeline_folder_id
        )
        if not latest_file:
            return "タイムラインのJSONファイルが見つかりません。"

        try:
            logs_by_date = await loop.run_in_executor(
                None, self._read_and_extract, service, latest_file["id"], {target_date}
            )
        except Exception as e:
            return f"JSON読み込みエラー: {e}"

        if not logs_by_date or target_date not in logs_by_date:
            return f"⚠️ `{latest_file['name']}` 内に **{target_date}** の移動データが見つかりませんでした。"

        await self._write_to_obsidian(
            target_date, logs_by_date[target_date], force=True
        )
        return f"✅ **{target_date}** の移動記録をObsidianに同期しました！"

    @tasks.loop(time=time(hour=23, minute=50, tzinfo=JST))
    async def process_timeline_json(self):
        logging.info("タイムラインJSONの自動処理を開始します。")
        loop = asyncio.get_running_loop()
        service = self.drive_service.get_service()
        if not service:
            return

        timeline_folder_id = await loop.run_in_executor(
            None, self._find_folder_in_root, service, "Timeline"
        )
        if not timeline_folder_id:
            return

        json_files = await loop.run_in_executor(
            None, self._get_unprocessed_json, service, timeline_folder_id
        )
        if not json_files:
            return

        lookback_days = 7
        today = datetime.now(JST).date()
        target_dates = {
            (today - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(lookback_days)
        }

        # 既に Location History が書かれている日は除外し、新規の日だけを抽出対象にする。
        # これにより既記録日の Places API 課金（force=False で書き込みはどのみちスキップ
        # されていた分）をまるごと無くす。
        recorded = await self._dates_with_location_history(service, target_dates)
        pending_dates = target_dates - recorded

        for file_info in json_files:
            file_id = file_info["id"]
            file_name = file_info["name"]

            processed_dates = []
            # 新規対象日が無いときは抽出（API 呼び出し）自体を行わず、ファイルの
            # リネーム（処理済み化）だけ行う。空集合を渡すと全日処理になる点に注意。
            if pending_dates:
                try:
                    logs_by_date = await loop.run_in_executor(
                        None, self._read_and_extract, service, file_id, pending_dates
                    )
                except Exception:
                    logs_by_date = None

                if logs_by_date:
                    today_str = datetime.now(JST).strftime("%Y-%m-%d")
                    for date_str, log_text in logs_by_date.items():
                        if await self._write_to_obsidian(date_str, log_text, force=False):
                            processed_dates.append(date_str)
                            # 外食らしき滞在を検知したら meal ログ質問を投下（事後の振り返り）
                            await self._maybe_ask_meal_from_location(date_str, log_text)
                            # 過去日の位置ログが遅れて届いた場合は、その日の「1日のまとめ」を
                            # 後から作り直す（後日まとめ）。今日の分は 22:00 の通常生成に任せる。
                            if date_str != today_str:
                                await self._maybe_regenerate_summary(date_str)

            timestamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
            await loop.run_in_executor(
                None,
                self._rename_file,
                service,
                file_id,
                f"処理済み_{timestamp}_{file_name}",
            )

            if processed_dates:
                dates_str = ", ".join(sorted(processed_dates))
                partner_cog = self.bot.get_cog("PartnerCog")
                if partner_cog:
                    context = f"ロケーション履歴を同期した日付: {dates_str}"
                    await partner_cog.generate_and_send_routine_message(
                        context,
                        PROMPT_LOCATION_SYNC + "\n\n出力の末尾に必ず `[ACTION:open_location_log]` を改行して追記してください。",
                    )
                else:
                    from api.notification_service import save_message_and_notify as _save_msg
                    await _save_msg(
                        "assistant",
                        f"📍 {dates_str} の移動記録を保存したよ！\n[ACTION:open_location_log]",
                        proactive=True,
                    )

    async def _maybe_regenerate_summary(self, date_str: str):
        """過去日の位置ログが遅れて届いたとき、その日のデイリーサマリーを作り直し、
        更新したことをユーザーへ通知する（後日まとめの自動反映）。"""
        try:
            from api.routers.daily_summary import regenerate_summary_for_date
            saved = await regenerate_summary_for_date(date_str)
        except Exception as e:
            logging.debug(f"location late-summary regenerate skipped ({date_str}): {e}")
            return
        if not saved:
            return
        try:
            from api.notification_service import save_message_and_notify
            await save_message_and_notify(
                "assistant",
                f"📅 {date_str} の位置ログが届いたので、その日のまとめを更新したよ！\n[ACTION:open_reflection]",
                proactive=True, title="📅 まとめ更新",
            )
        except Exception as e:
            logging.debug(f"location late-summary notify skipped ({date_str}): {e}")

    async def _maybe_ask_meal_from_location(self, date_str: str, log_text: str):
        """その日の滞在記録から、食事時間帯の外食らしき滞在を検知して
        『〇〇で何を食べた？』の meal ログ質問を投下する（事後の振り返り）。
        対象は今日のみ。自宅・勤務先・不明は除外。最初の1件だけ。"""
        if date_str != datetime.now(JST).strftime("%Y-%m-%d"):
            return
        import re as _re
        exclude = {"自宅", "勤務先", "不明な場所"}
        pat = _re.compile(r"- \*\*(\d{1,2}):(\d{2}) - \d{1,2}:\d{2}\*\* \([^)]*\) 滞在: (.+)")
        hit = None
        for m in pat.finditer(log_text or ""):
            hour = int(m.group(1))
            place = (m.group(3) or "").strip()
            if not place or place in exclude:
                continue
            if 11 <= hour < 15:
                hit = ("昼", place)
                break
            if 17 <= hour < 22:
                hit = ("夜", place)
                break
        if not hit:
            return
        when, place = hit
        # 「確認するだけ」ログ：入力させず、✓ボタン1タップで外食を記録できる確認メッセージを送る。
        safe_place = place.replace("|", " ").replace("=", " ").strip()
        meal_type = "昼食" if when == "昼" else "夕食"
        msg = (
            f"🍽 {when}に「{safe_place}」にいたみたいだね。外食した？\n"
            f"[ACTION:meal_quick:name={safe_place}|meal_type={meal_type}]"
        )
        try:
            from api.notification_service import save_message_and_notify
            await save_message_and_notify("assistant", msg, proactive=True, title="🍽 外食の記録")
        except Exception as e:
            logging.debug(f"location meal confirm error: {e}")

    @process_timeline_json.before_loop
    async def before_process(self):
        await self.bot.wait_until_ready()

    # ==========================================================
    # 毎日 22:30 にロケーション保存をユーザーへリマインドする
    # ==========================================================
    _last_save_reminder_date = None

    @tasks.loop(minutes=1)
    async def location_save_reminder_task(self):
        from services.schedule_resolver import is_due
        due, today = await is_due(
            "location_save_reminder", "22:30", "daily",
            self._last_save_reminder_date,
        )
        if not due:
            return
        self._last_save_reminder_date = today
        partner_cog = self.bot.get_cog("PartnerCog")
        if not partner_cog:
            return
        instruction = (
            "次の文章をユーザーに優しいタメ口で送信してください。改変せずほぼそのまま送ってください。\n\n"
            "📍 今日のロケーション履歴をエクスポートしてDriveの`Timeline/`フォルダにアップロードしておいてね！"
            "（Google Maps タイムライン → 共有 → JSON エクスポート）\n"
            "23:55のデイリー整理までに保存しておけば、移動記録が自動で今日のノートに反映されるよ🌙\n"
            "[ACTION:open_location_log]"
        )
        try:
            await partner_cog.generate_and_send_routine_message("", instruction)
        except Exception as e:
            logging.error(f"location save reminder send error: {e}")

    @location_save_reminder_task.before_loop
    async def before_save_reminder(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(LocationLogCog(bot))
