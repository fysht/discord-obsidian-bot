# ---------------------------------------------------------
# 1. インポート処理の整理
# ---------------------------------------------------------
import os
import logging
import json
from datetime import datetime, time, timedelta
import io
import asyncio
import re

import discord
from discord.ext import commands, tasks
from discord import app_commands
import googlemaps
from geopy.distance import great_circle
from googleapiclient.http import MediaIoBaseDownload

# ---------------------------------------------------------
# ローカルモジュールのインポートと定数設定
# ---------------------------------------------------------
from config import JST
from utils.obsidian_utils import update_section

DATE_REGEX = re.compile(r'^\d{4}-\d{2}-\d{2}$')

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
        self.memo_channel_id = int(os.getenv("MEMO_CHANNEL_ID", 0))
        self.drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        
        # ---------------------------------------------------------
        # 統合されたDriveサービスを利用
        # ---------------------------------------------------------
        self.drive_service = bot.drive_service
        
        self.home_coordinates = self._parse_coordinates(os.getenv("HOME_COORDINATES"))
        self.work_coordinates = self._parse_coordinates(os.getenv("WORK_COORDINATES"))
        self.exclude_radius_meters = int(os.getenv("EXCLUDE_RADIUS_METERS", 500))
        self.google_places_api_key = os.getenv("GOOGLE_PLACES_API_KEY")
        
        self.gmaps = googlemaps.Client(key=self.google_places_api_key) if self.google_places_api_key else None
        
        self.process_timeline_json.start()

    def cog_unload(self):
        self.process_timeline_json.cancel()

    # --- ヘルパーメソッド ---
    def _get_place_name_from_id(self, place_id: str) -> str:
        if not self.gmaps: return f"場所ID: {place_id}"
        try:
            place_details = self.gmaps.place(place_id=place_id, language='ja')
            if place_details and 'result' in place_details and 'name' in place_details['result']:
                return place_details['result']['name']
        except Exception as e:
            logging.error(f"Places APIからの名前取得に失敗: {e}")
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

    # --- Google Drive 固有処理 (専用の操作のみ残す) ---
    def _find_folder_in_root(self, service, name):
        query = f"'root' in parents and name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        res = service.files().list(q=query, fields="files(id)").execute()
        files = res.get('files', [])
        return files[0]['id'] if files else None

    def _get_unprocessed_json(self, service, folder_id):
        query = f"'{folder_id}' in parents and name contains 'タイムライン.json' and not name contains '処理済み_' and trashed = false"
        res = service.files().list(q=query, fields="files(id, name)").execute()
        return res.get('files', [])

    def _get_latest_timeline_json(self, service, folder_id):
        query = f"'{folder_id}' in parents and name contains 'タイムライン.json' and trashed = false"
        res = service.files().list(q=query, fields="files(id, name, createdTime)", orderBy="createdTime desc").execute()
        files = res.get('files', [])
        return files[0] if files else None

    def _rename_file(self, service, file_id, new_name):
        service.files().update(fileId=file_id, body={'name': new_name}).execute()

    def _read_json(self, service, file_id):
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=file_id))
        done = False
        while not done: _, done = downloader.next_chunk()
        return json.loads(fh.getvalue().decode('utf-8'))

    # --- JSONデータ解析の共通ロジック ---
    def _extract_logs_from_json(self, data: dict, target_dates: set[str] = None) -> dict:
        """
        target_dates に含まれる日付のデータのみを抽出する。
        """
        segments = data.get("semanticSegments", [])
        if not segments: return None

        events_by_date = {}
        for seg in segments:
            start_time = self._parse_iso_timestamp(seg.get("startTime", ''))
            end_time = self._parse_iso_timestamp(seg.get("endTime", ''))
            if not start_time or not end_time: continue

            event_date = start_time.astimezone(JST).date()
            date_str = event_date.strftime('%Y-%m-%d')

            # 対象日付セットが指定されていて、かつそれに含まれない場合はスキップ
            if target_dates and date_str not in target_dates: continue

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

        # テキスト整形
        logs_by_date = {}
        for d_str, events in sorted(events_by_date.items()):
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
            
            logs_by_date[d_str] = "\n".join(log_entries)

        return logs_by_date

    # --- Obsidian書き込みの共通ロジック ---
    async def _write_to_obsidian(self, date_str: str, log_text: str, force: bool = False) -> bool:
        """
        Obsidianに書き込む。すでに記入済みの場合はスキップするが、force=Trueの場合は上書きする。
        書き込みを実行した場合はTrueを返す。
        """
        service = self.drive_service.get_service()
        if not service: return False

        daily_folder = await self.drive_service.find_file(service, self.drive_folder_id, "DailyNotes")
        if not daily_folder:
            daily_folder = await self.drive_service.create_folder(service, self.drive_folder_id, "DailyNotes")

        daily_file = await self.drive_service.find_file(service, daily_folder, f"{date_str}.md")
        
        cur = ""
        if daily_file:
            try:
                cur = await self.drive_service.read_text_file(service, daily_file)
            except Exception as e:
                logging.error(f"ファイル読み込みエラー: {e}")

        # ★ 変更: 強制フラグがない場合、既にロケーション履歴（箇条書きの - ）が存在すればスキップ
        if not force and re.search(r'## 📍 Location History\s*-', cur):
            return False

        if not cur:
            cur = f"---\ndate: {date_str}\n---\n\n# Daily Note {date_str}\n\n## 📍 Location History\n\n"
        
        new = update_section(cur, log_text, "## 📍 Location History")
        
        if daily_file:
            await self.drive_service.update_text(service, daily_file, new)
        else:
            await self.drive_service.upload_text(service, daily_folder, f"{date_str}.md", new)
            
        return True


    # ▼ 毎日 23:50 に全自動で実行される処理
    @tasks.loop(time=time(hour=23, minute=50, tzinfo=JST))
    async def process_timeline_json(self):
        logging.info("タイムラインJSONの自動処理を開始します。")
        loop = asyncio.get_running_loop()
        service = self.drive_service.get_service()
        if not service: return

        channel = self.bot.get_channel(self.memo_channel_id)

        timeline_folder_id = await loop.run_in_executor(None, self._find_folder_in_root, service, "Timeline")
        if not timeline_folder_id: return

        json_files = await loop.run_in_executor(None, self._get_unprocessed_json, service, timeline_folder_id)
        if not json_files: return 

        # ★ 変更: 過去何日分を遡って補完処理するかを指定（今回は7日間）
        lookback_days = 7
        today = datetime.now(JST).date()
        target_dates = { (today - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(lookback_days) }

        for file_info in json_files:
            file_id = file_info['id']
            file_name = file_info['name']
            
            try:
                data = await loop.run_in_executor(None, self._read_json, service, file_id)
            except Exception as e:
                logging.error(f"JSON読み込みエラー: {e}")
                continue

            # 過去7日分の日付だけを抽出
            logs_by_date = self._extract_logs_from_json(data, target_dates=target_dates)

            processed_dates = []
            if logs_by_date:
                # 抽出された日付ごとに書き込みを試みる
                for date_str, log_text in logs_by_date.items():
                    # すでに記入済みの日はスキップされ、未処理の日だけ書き込まれる
                    was_written = await self._write_to_obsidian(date_str, log_text, force=False)
                    if was_written:
                        processed_dates.append(date_str)

            # JSONファイルを「処理済み」にリネーム
            timestamp = datetime.now(JST).strftime('%Y%m%d_%H%M%S')
            await loop.run_in_executor(None, self._rename_file, service, file_id, f"処理済み_{timestamp}_{file_name}")
            
            if channel and processed_dates:
                # 処理した日付を昇順に並べ替えて通知
                dates_str = ", ".join(sorted(processed_dates))
                await channel.send(f"📍 未処理のロケーション履歴を解析し、以下の日付のデータをObsidianに保存しました！\n({dates_str})")


    # ▼ 手動で過去のデータを同期するコマンド
    @app_commands.command(name="location_sync", description="過去のロケーション履歴を指定して手動で同期します。")
    @app_commands.describe(target_date="同期したい日付 (例: 2026-02-15)")
    async def sync_location_manual(self, interaction: discord.Interaction, target_date: str):
        await interaction.response.defer(ephemeral=False)
        
        if not DATE_REGEX.match(target_date):
            await interaction.followup.send("❌ 日付の形式が正しくありません。(例: 2026-02-15)")
            return

        loop = asyncio.get_running_loop()
        service = self.drive_service.get_service()
        if not service:
            await interaction.followup.send("❌ Google Drive APIの認証に失敗しました。")
            return

        timeline_folder_id = await loop.run_in_executor(None, self._find_folder_in_root, service, "Timeline")
        if not timeline_folder_id:
            await interaction.followup.send("❌ マイドライブに `Timeline` フォルダが見つかりません。")
            return

        latest_file = await loop.run_in_executor(None, self._get_latest_timeline_json, service, timeline_folder_id)
        if not latest_file:
            await interaction.followup.send("❌ `Timeline` フォルダにJSONファイルが見つかりません。")
            return

        try:
            data = await loop.run_in_executor(None, self._read_json, service, latest_file['id'])
        except Exception as e:
            await interaction.followup.send(f"❌ ファイルの読み込みに失敗しました。({e})")
            return

        # 手動実行の場合は、指定された1日分だけを抽出
        logs_by_date = self._extract_logs_from_json(data, target_dates={target_date})
        
        if not logs_by_date or target_date not in logs_by_date:
            await interaction.followup.send(f"⚠️ 参照したファイル（`{latest_file['name']}`）内に **{target_date}** の移動データが見つかりませんでした。")
            return

        # 手動実行なので、記入済みであっても強制的に上書き (force=True)
        await self._write_to_obsidian(target_date, logs_by_date[target_date], force=True)
        await interaction.followup.send(f"✅ **{target_date}** のロケーション履歴を手動同期して保存しました！\n(参照ファイル: `{latest_file['name']}`)")


    @process_timeline_json.before_loop
    async def before_process(self):
        await self.bot.wait_until_ready()

async def setup(bot): 
    await bot.add_cog(LocationLogCog(bot))