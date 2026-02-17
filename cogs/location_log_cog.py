import os
import discord
from discord.ext import commands, tasks
import logging
import json
from datetime import datetime, time
import zoneinfo
import io
import asyncio
import googlemaps
from geopy.distance import great_circle

# Google Drive API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from utils.obsidian_utils import update_section

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
TOKEN_FILE = 'token.json'
SCOPES = ['https://www.googleapis.com/auth/drive']

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
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = int(os.getenv("LOCATION_LOG_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        
        # 以前のコードの環境変数を継承
        self.home_coordinates = self._parse_coordinates(os.getenv("HOME_COORDINATES"))
        self.work_coordinates = self._parse_coordinates(os.getenv("WORK_COORDINATES"))
        self.exclude_radius_meters = int(os.getenv("EXCLUDE_RADIUS_METERS", 500))
        self.google_places_api_key = os.getenv("GOOGLE_PLACES_API_KEY")
        
        self.gmaps = googlemaps.Client(key=self.google_places_api_key) if self.google_places_api_key else None
        
        # 毎日 23:50 に自動で処理を開始する
        self.process_timeline_json.start()

    def cog_unload(self):
        self.process_timeline_json.cancel()

    # --- ヘルパーメソッド（以前のコードから移植） ---
    def _get_place_name_from_id(self, place_id: str) -> str:
        if not self.gmaps: return f"場所ID: {place_id}"
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

    # --- Google Drive API 関連メソッド ---
    def _get_drive_service(self):
        creds = None
        if os.path.exists(TOKEN_FILE):
            try: creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            except: pass
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try: creds.refresh(Request()); open(TOKEN_FILE,'w').write(creds.to_json())
                except: return None
            else: return None
        return build('drive', 'v3', credentials=creds)

    def _find_file_recursive(self, service, parent_id, name, mime_type=None):
        query = f"'{parent_id}' in parents and name = '{name}' and trashed = false"
        if mime_type: query += f" and mimeType = '{mime_type}'"
        res = service.files().list(q=query, fields="files(id)").execute()
        files = res.get('files', [])
        return files[0]['id'] if files else None

    def _find_folder_in_root(self, service, name):
        query = f"'root' in parents and name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        res = service.files().list(q=query, fields="files(id)").execute()
        files = res.get('files', [])
        return files[0]['id'] if files else None

    def _get_unprocessed_json(self, service, folder_id):
        query = f"'{folder_id}' in parents and name contains 'Timeline.json' and not name contains '処理済み_' and trashed = false"
        res = service.files().list(q=query, fields="files(id, name)").execute()
        return res.get('files', [])

    def _rename_file(self, service, file_id, new_name):
        service.files().update(fileId=file_id, body={'name': new_name}).execute()

    def _read_json(self, service, file_id):
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=file_id))
        done = False
        while not done: _, done = downloader.next_chunk()
        return json.loads(fh.getvalue().decode('utf-8'))

    def _read_text(self, service, file_id):
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=file_id))
        done = False
        while not done: _, done = downloader.next_chunk()
        return fh.getvalue().decode('utf-8')

    def _update_text(self, service, file_id, content):
        service.files().update(fileId=file_id, media_body=MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')).execute()

    def _create_text(self, service, parent_id, name, content):
        service.files().create(body={'name': name, 'parents': [parent_id], 'mimeType': 'text/markdown'}, media_body=MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown')).execute()

    # ▼ 毎日 23:50 に全自動で実行される処理
    @tasks.loop(time=time(hour=23, minute=50, tzinfo=JST))
    async def process_timeline_json(self):
        logging.info("タイムラインJSONの自動処理を開始します。")
        loop = asyncio.get_running_loop()
        service = await loop.run_in_executor(None, self._get_drive_service)
        if not service: return

        channel = self.bot.get_channel(self.channel_id)

        # 1. マイドライブ直下の「Timeline」フォルダを探す
        timeline_folder_id = await loop.run_in_executor(None, self._find_folder_in_root, service, "Timeline")
        if not timeline_folder_id:
            if channel: await channel.send("⚠️ 定期実行エラー：マイドライブに `Timeline` フォルダが見つかりません。")
            return

        # 2. 未処理の Timeline.json を取得
        json_files = await loop.run_in_executor(None, self._get_unprocessed_json, service, timeline_folder_id)
        if not json_files: return 

        for file_info in json_files:
            file_id = file_info['id']
            file_name = file_info['name']
            
            if channel: await channel.send(f"📄 新しいタイムラインデータ (`{file_name}`) を見つけたので自動解析するね！")

            # 3. JSONファイルをダウンロードして読み込む
            try:
                data = await loop.run_in_executor(None, self._read_json, service, file_id)
            except Exception as e:
                logging.error(f"JSON読み込みエラー: {e}")
                if channel: await channel.send(f"❌ `{file_name}` の読み込みに失敗しました。")
                continue

            # 4. JSONデータの解析と日付ごとの振り分け（移植ロジック）
            segments = data.get("semanticSegments", [])
            if not segments:
                if channel: await channel.send(f"⚠️ `{file_name}` 内に 'semanticSegments' が見つかりませんでした。データ形式が異なる可能性があります。")
                continue

            events_by_date = {}
            for seg in segments:
                start_time = self._parse_iso_timestamp(seg.get("startTime", ''))
                end_time = self._parse_iso_timestamp(seg.get("endTime", ''))
                if not start_time or not end_time: continue

                event_date = start_time.astimezone(JST).date()
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

            # --- Obsidian書き込み用のテキスト整形 ---
            logs_by_date = {}
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
                
                logs_by_date[date_str] = log_entries

            # 5. ボット用フォルダ内の DailyNotes を探す
            daily_folder = await loop.run_in_executor(None, self._find_file_recursive, service, self.drive_folder_id, "DailyNotes", "application/vnd.google-apps.folder")
            if not daily_folder:
                meta = {'name': 'DailyNotes', 'mimeType': 'application/vnd.google-apps.folder', 'parents': [self.drive_folder_id]}
                folder_obj = await loop.run_in_executor(None, lambda: service.files().create(body=meta, fields='id').execute())
                daily_folder = folder_obj.get('id')

            # 6. 日付ごとにノートを作成・追記する
            for date_str, logs in logs_by_date.items():
                log_text = "\n".join(logs)
                daily_file = await loop.run_in_executor(None, self._find_file_recursive, service, daily_folder, f"{date_str}.md")
                
                cur = ""
                if daily_file:
                    cur = await loop.run_in_executor(None, self._read_text, service, daily_file)
                else:
                    cur = f"---\ntitle: {date_str}\ndate: {date_str}\n---\n\n# {date_str}\n\n## 📍 Location Logs\n\n"
                
                new = update_section(cur, log_text, "## 📍 Location Logs")
                
                if daily_file:
                    await loop.run_in_executor(None, self._update_text, service, daily_file, new)
                else:
                    await loop.run_in_executor(None, self._create_text, service, daily_folder, f"{date_str}.md", new)

            # 7. 処理済みのファイルをリネーム（処理日時をつけて重複を防ぐ）
            timestamp = datetime.now(JST).strftime('%Y%m%d_%H%M%S')
            await loop.run_in_executor(None, self._rename_file, service, file_id, f"処理済み_{timestamp}_{file_name}")
            
            if channel:
                processed_dates = ", ".join(logs_by_date.keys())
                await channel.send(f"✅ `{file_name}` の自動処理が完了したよ！（更新した日付: {processed_dates}）")

    @process_timeline_json.before_loop
    async def before_process(self):
        await self.bot.wait_until_ready()

async def setup(bot): await bot.add_cog(LocationLogCog(bot))