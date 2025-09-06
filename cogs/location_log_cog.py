import os
import discord
from discord.ext import commands
import logging
import json
from datetime import datetime, time
import zoneinfo
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
from geopy.distance import great_circle
import re
import googlemaps

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
# 期間指定(YYYY-MM-DD~YYYY-MM-DD)も可能な正規表現
DATE_RANGE_REGEX = re.compile(r'^(\d{4}-\d{2}(?:-\d{2})?)(?:~(\d{4}-\d{2}-\d{2}))?$')

SECTION_ORDER = [
    "## Health Metrics", "## Location Logs", "## WebClips", "## YouTube Summaries",
    "## AI Logs", "## Zero-Second Thinking", "## Memo"
]

# 絵文字を削除した移動手段の辞書
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
    "UNKNOWN": "不明な移動"
}

class LocationLogCog(commands.Cog):
    """Google Takeoutのロケーション履歴を解析し、Obsidianに記録するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # .envからの設定読み込み
        self.location_log_channel_id = int(os.getenv("LOCATION_LOG_CHANNEL_ID", 0))
        self.json_path_in_dropbox = os.getenv("LOCATION_HISTORY_JSON_PATH")
        self.home_coordinates = self._parse_coordinates(os.getenv("HOME_COORDINATES"))
        self.work_coordinates = self._parse_coordinates(os.getenv("WORK_COORDINATES"))
        self.exclude_radius_meters = int(os.getenv("EXCLUDE_RADIUS_METERS", 500))

        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        
        self.google_places_api_key = os.getenv("GOOGLE_PLACES_API_KEY")

        self.is_ready = self._validate_and_init_clients()

    def _validate_and_init_clients(self):
        if not all([self.location_log_channel_id, self.dropbox_refresh_token, self.json_path_in_dropbox, self.google_places_api_key]):
            logging.error("LocationLogCog: 必須の環境変数が不足しています。")
            return False
        
        self.dbx = dropbox.Dropbox(
            oauth2_refresh_token=self.dropbox_refresh_token,
            app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret
        )
        self.gmaps = googlemaps.Client(key=self.google_places_api_key)
        return True
    
    def _get_place_name_from_id(self, place_id: str) -> str:
        try:
            place_details = self.gmaps.place(place_id=place_id, language='ja')
            if place_details and 'result' in place_details and 'name' in place_details['result']:
                return place_details['result']['name']
        except Exception as e:
            logging.error(f"Places APIからの名前取得に失敗 (Place ID: {place_id}): {e}")
        return f"場所ID: {place_id}"

    def _parse_coordinates(self, coord_str: str | None) -> tuple[float, float] | None:
        if not coord_str: return None
        try:
            lat, lon = map(float, coord_str.split(','))
            return (lat, lon)
        except (ValueError, TypeError):
            logging.error(f"座標の解析に失敗しました: {coord_str}")
            return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.is_ready or message.author.bot or message.channel.id != self.location_log_channel_id:
            return
        content = message.content.strip()
        if (date_match := DATE_RANGE_REGEX.match(content)):
            start_date_str = date_match.group(1)
            end_date_str = date_match.group(2) # 期間指定の終了日

            await message.add_reaction("⏳")
            try:
                json_data = await self._download_master_json()
                if json_data:
                    await self._process_location_history(json_data, message, start_date_str, end_date_str)
                else:
                    await message.reply("❌ Dropboxからロケーション履歴ファイルを取得できませんでした。パスが正しいか確認してください。")
                    await message.add_reaction("❌")
            except Exception as e:
                logging.error(f"ロケーション履歴の処理中にエラーが発生: {e}", exc_info=True)
                await message.reply(f"❌ エラーが発生しました: {e}")
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("❌")

    async def _download_master_json(self) -> dict | None:
        logging.info(f"マスターJSONファイルをDropboxからダウンロードします: {self.json_path_in_dropbox}")
        try:
            _, res = self.dbx.files_download(self.json_path_in_dropbox)
            return json.loads(res.content.decode('utf-8'))
        except ApiError as e:
            logging.error(f"マスターJSONファイルのダウンロードに失敗: {e}")
            return None

    def _format_duration(self, duration_seconds: float) -> str:
        minutes = int(duration_seconds / 60)
        if minutes < 1: return "1分未満"
        hours, minutes = divmod(minutes, 60)
        if hours > 0: return f"{hours}時間{minutes}分"
        return f"{minutes}分"

    def _parse_iso_timestamp(self, ts_str: str) -> datetime | None:
        try:
            if ts_str.count(':') == 3:
                ts_str = ts_str[::-1].replace(':', '', 1)[::-1]
            return datetime.fromisoformat(ts_str)
        except (ValueError, TypeError): return None

    async def _process_location_history(self, data: dict, message: discord.Message, start_date_str: str, end_date_str: str | None):
        segments = data.get("semanticSegments", [])
        if not segments:
            await message.reply("❌ JSONファイル内に'semanticSegments'が見つかりませんでした。")
            return
        
        # --- 日付フィルタの準備 ---
        try:
            start_filter_dt = datetime.strptime(start_date_str, '%Y-%m-%d' if len(start_date_str) > 7 else '%Y-%m').date()
            if end_date_str:
                end_filter_dt = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            else:
                end_filter_dt = start_filter_dt if len(start_date_str) > 7 else None # 日 or 月でフィルタ
        except ValueError:
            await message.reply("❌ 日付の形式が正しくありません。(例: 2025-08, 2025-08-15, 2025-08-01~2025-08-10)")
            return

        events_by_date = {}
        logging.info(f"合計 {len(segments)} 個のセグメントを処理します。フィルタ: {start_date_str} ~ {end_date_str or ''}")

        for seg in segments:
            start_time = self._parse_iso_timestamp(seg.get("startTime", ''))
            end_time = self._parse_iso_timestamp(seg.get("endTime", ''))
            if not start_time or not end_time: continue

            event_date = start_time.astimezone(JST).date()

            # --- 日付フィルタリングロジック ---
            if end_filter_dt: # 期間指定 (YYYY-MM-DD ~ YYYY-MM-DD)
                if not (start_filter_dt <= event_date <= end_filter_dt):
                    continue
            elif len(start_date_str) > 7: # 日指定 (YYYY-MM-DD)
                if event_date != start_filter_dt:
                    continue
            else: # 月指定 (YYYY-MM)
                if event_date.year != start_filter_dt.year or event_date.month != start_filter_dt.month:
                    continue
            
            date_str = event_date.strftime('%Y-%m-%d')
            events_by_date.setdefault(date_str, [])

            duration_seconds = (end_time - start_time).total_seconds()
            duration_formatted = self._format_duration(duration_seconds)
            event = {"start": start_time, "end": end_time}

            if (visit_data := seg.get("visit")):
                top_candidate = visit_data.get("topCandidate", {})
                lat_lng_str = top_candidate.get("placeLocation", {}).get("latLng")
                if not lat_lng_str: continue
                try:
                    lat_str, lon_str = lat_lng_str.replace('°', '').split(',')
                    place_coords = (float(lat_str), float(lon_str.strip()))
                except (ValueError, IndexError): continue
                
                place_name = "不明な場所"
                place_id = top_candidate.get('placeId')
                if place_id: place_name = self._get_place_name_from_id(place_id)
                
                if self.home_coordinates and great_circle(place_coords, self.home_coordinates).meters < self.exclude_radius_meters: place_name = "自宅"
                elif self.work_coordinates and great_circle(place_coords, self.work_coordinates).meters < self.exclude_radius_meters: place_name = "勤務先"
                
                event.update({"type": "stay", "name": place_name, "duration": duration_formatted})
                events_by_date[date_str].append(event)
            
            elif (activity_data := seg.get("activity")):
                activity_type = activity_data.get("topCandidate", {}).get("type", "UNKNOWN")
                distance_m = activity_data.get("distanceMeters", 0)
                distance_km_str = f" (約{distance_m / 1000:.1f}km)" if distance_m > 0 else ""
                event.update({"type": "move", "activity": ACTIVITY_TYPE_MAP.get(activity_type, "不明な移動"), "duration": duration_formatted, "distance": distance_km_str})
                events_by_date[date_str].append(event)

        if not any(events_by_date.values()):
            await message.reply(f"✅ 期間 '{message.content.strip()}' 内に処理対象となる行動記録は見つかりませんでした。")
        else:
            for date_str, events in sorted(events_by_date.items()):
                if not events: continue
                sorted_events = sorted(events, key=lambda x: x['start'])
                log_entries, last_place = [], None
                for event in sorted_events:
                    start_str_jst = event['start'].astimezone(JST).strftime('%H:%M')
                    if event['type'] == 'stay':
                        if last_place is not None: log_entries.append(f"- **{start_str_jst}** {event['name']}に到着")
                        log_entries.append(f"- **{start_str_jst} - {event['end'].astimezone(JST).strftime('%H:%M')}** ({event['duration']}) 滞在: {event['name']}")
                        last_place = event['name']
                    elif event['type'] == 'move':
                        if last_place: log_entries.append(f"- **{start_str_jst}** {last_place}を出発")
                        log_entries.append(f"- **{start_str_jst} - {event['end'].astimezone(JST).strftime('%H:%M')}** ({event['duration']}) {event['activity']}{event['distance']}")
                        last_place = None
                await self._save_data_to_obsidian(date_str, "\n".join(log_entries))
            
            start_date, end_date = min(events_by_date.keys()), max(events_by_date.keys())
            await message.reply(f"✅ 詳細な行動履歴の記録が完了しました！ (対象期間: {start_date} ~ {end_date})")

        await message.remove_reaction("⏳", self.bot.user)
        await message.add_reaction("✅")

    def _update_daily_note_with_ordered_section(self, current_content: str, text_to_add: str, section_header: str) -> str:
        lines = current_content.split('\n')
        try:
            header_index = lines.index(section_header)
            end_index = header_index + 1
            while end_index < len(lines) and not lines[end_index].strip().startswith('## '):
                end_index += 1
            del lines[header_index + 1 : end_index]
            lines.insert(header_index + 1, text_to_add)
            return "\n".join(lines)
        except ValueError:
            new_section_with_header = f"\n{section_header}\n{text_to_add}"
            if not any(s in current_content for s in SECTION_ORDER):
                 return current_content.strip() + new_section_with_header
            existing_sections = {line.strip(): i for i, line in enumerate(lines) if line.strip() in SECTION_ORDER}
            try: new_section_order_index = SECTION_ORDER.index(section_header)
            except ValueError: return current_content.strip() + new_section_with_header
            insert_after_index = -1
            for i in range(new_section_order_index - 1, -1, -1):
                preceding_header = SECTION_ORDER[i]
                if preceding_header in existing_sections:
                    header_line_index = existing_sections[preceding_header]
                    insert_after_index = header_line_index + 1
                    while insert_after_index < len(lines) and not lines[insert_after_index].strip().startswith('## '):
                        insert_after_index += 1
                    break
            if insert_after_index != -1:
                lines.insert(insert_after_index, new_section_with_header)
                return "\n".join(lines).strip()
            insert_before_index = -1
            for i in range(new_section_order_index + 1, len(SECTION_ORDER)):
                following_header = SECTION_ORDER[i]
                if following_header in existing_sections:
                    insert_before_index = existing_sections[following_header]
                    break
            if insert_before_index != -1:
                lines.insert(insert_before_index, new_section_with_header + "\n")
                return "\n".join(lines).strip()
            return current_content.strip() + new_section_with_header

    async def _save_data_to_obsidian(self, date_str: str, log_text: str):
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        try:
            _, res = self.dbx.files_download(daily_note_path)
            current_content = res.content.decode('utf-8')
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                current_content = ""
            else: raise
        new_daily_content = self._update_daily_note_with_ordered_section(
            current_content, log_text, "## Location Logs"
        )
        self.dbx.files_upload(
            new_daily_content.encode('utf-8'), daily_note_path, mode=WriteMode('overwrite')
        )
        logging.info(f"LocationLogCog: {daily_note_path} を更新しました。")

async def setup(bot: commands.Bot):
    await bot.add_cog(LocationLogCog(bot))