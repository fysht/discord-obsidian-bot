import os
import discord
from discord.ext import commands
import logging
import json
from datetime import datetime, timedelta
import zoneinfo
import dropbox
from dropbox.files import WriteMode, DownloadError
from dropbox.exceptions import ApiError
from geopy.distance import great_circle
import re

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
# 日付指定の正規表現 (YYYY-MM or YYYY-MM-DD)
DATE_RANGE_REGEX = re.compile(r'^\d{4}-\d{2}(?:-\d{2})?$')

SECTION_ORDER = [
    "## Health Metrics",
    "## Location Logs",
    "## WebClips",
    "## YouTube Summaries",
    "## AI Logs",
    "## Zero-Second Thinking",
    "## Memo"
]

class LocationLogCog(commands.Cog):
    """Google Takeoutのロケーション履歴を解析し、Obsidianに記録するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # --- .envからの設定読み込み ---
        self.location_log_channel_id = int(os.getenv("LOCATION_LOG_CHANNEL_ID", 0))
        self.json_path_in_dropbox = os.getenv("LOCATION_HISTORY_JSON_PATH")
        self.home_coordinates = self._parse_coordinates(os.getenv("HOME_COORDINATES"))
        self.work_coordinates = self._parse_coordinates(os.getenv("WORK_COORDINATES"))
        self.exclude_radius_meters = int(os.getenv("EXCLUDE_RADIUS_METERS", 500))

        self.dropbox_app_key = os.getenv("DROPBOX_APP_KEY")
        self.dropbox_app_secret = os.getenv("DROPBOX_APP_SECRET")
        self.dropbox_refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        self.dropbox_vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")

        self.is_ready = self._validate_and_init_clients()
        if not self.is_ready:
            logging.error("LocationLogCog: 環境変数が不足しているため、初期化に失敗しました。")

    def _validate_and_init_clients(self):
        """環境変数のチェックとAPIクライアントの初期化を行う"""
        if not all([self.location_log_channel_id, self.dropbox_refresh_token, self.json_path_in_dropbox]):
            return False
        
        self.dbx = dropbox.Dropbox(
            oauth2_refresh_token=self.dropbox_refresh_token,
            app_key=self.dropbox_app_key, app_secret=self.dropbox_app_secret
        )
        return True
    
    def _parse_coordinates(self, coord_str: str | None) -> tuple[float, float] | None:
        """ "緯度,経度" 形式の文字列をタプルに変換する """
        if not coord_str:
            return None
        try:
            lat, lon = map(float, coord_str.split(','))
            return (lat, lon)
        except (ValueError, TypeError):
            logging.error(f"座標の解析に失敗しました: {coord_str}")
            return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """期間が投稿されたら、DropboxからJSONを読み込んで処理する"""
        if not self.is_ready or message.author.bot or message.channel.id != self.location_log_channel_id:
            return

        content = message.content.strip()
        
        if (date_match := DATE_RANGE_REGEX.match(content)):
            date_filter = date_match.group(0)
            
            await message.add_reaction("⏳")
            try:
                json_data = await self._download_master_json()
                if json_data:
                    await self._process_location_history(json_data, message, date_filter)
                else:
                    await message.reply("❌ Dropboxからロケーション履歴ファイルを取得できませんでした。パスが正しいか確認してください。")
                    await message.add_reaction("❌")
            except Exception as e:
                logging.error(f"ロケーション履歴の処理中にエラーが発生: {e}", exc_info=True)
                await message.reply(f"❌ エラーが発生しました: {e}")
                await message.remove_reaction("⏳", self.bot.user)
                await message.add_reaction("❌")

    async def _download_master_json(self) -> dict | None:
        """Dropboxの固定パスからマスターJSONファイルをダウンロードして解析する"""
        logging.info(f"マスターJSONファイルをDropboxからダウンロードします: {self.json_path_in_dropbox}")
        try:
            _, res = self.dbx.files_download(self.json_path_in_dropbox)
            content_bytes = res.content
            return json.loads(content_bytes.decode('utf-8'))
        except ApiError as e:
            logging.error(f"マスターJSONファイルのダウンロードに失敗: {e}")
            return None

    async def _process_location_history(self, data: dict, message: discord.Message, date_filter: str | None):
        """ロケーション履歴データを処理し、Obsidianに保存するメインロジック"""
        
        # ★ 新旧のJSON形式に対応
        timeline_objects = data.get("timelineObjects", [])
        if not timeline_objects:
            timeline_objects = data.get("semanticSegments", [])
            logging.info("`timelineObjects`が見つからなかったため、`semanticSegments`を処理します。")

        if not timeline_objects:
            logging.warning("JSONファイル内に `timelineObjects` も `semanticSegments` も見つかりませんでした。")
            await message.reply("❌ JSONファイルのデータ形式が不明です。処理を中断しました。")
            return

        visits_by_date = {}
        logging.info(f"合計 {len(timeline_objects)} 個のオブジェクトを処理します。フィルタ: {date_filter}")

        for i, obj in enumerate(timeline_objects):
            if "placeVisit" not in obj:
                continue

            visit = obj["placeVisit"]
            
            # タイムスタンプのキーが異なる場合に対応
            duration = visit.get("duration", {})
            start_ts_ms_str = duration.get("startTimestampMs") or duration.get("startTimestamp")
            end_ts_ms_str = duration.get("endTimestampMs") or duration.get("endTimestamp")

            if not start_ts_ms_str or not end_ts_ms_str:
                logging.debug(f"[{i}] スキップ: タイムスタンプが不完全です。")
                continue
            
            start_ts_ms = int(re.sub(r'\.\d+Z$', 'Z', start_ts_ms_str.replace('Z', ''))) if isinstance(start_ts_ms_str, str) else int(start_ts_ms_str)
            end_ts_ms = int(re.sub(r'\.\d+Z$', 'Z', end_ts_ms_str.replace('Z', ''))) if isinstance(end_ts_ms_str, str) else int(end_ts_ms_str)

            start_time = datetime.fromtimestamp(start_ts_ms / 1000, tz=JST)
            
            # 日付フィルタリング
            if date_filter and not start_time.strftime('%Y-%m-%d').startswith(date_filter):
                continue
            
            duration_ms = end_ts_ms - start_ts_ms
            if duration_ms < 30 * 60 * 1000:
                logging.debug(f"[{i}] スキップ: 滞在時間不足 ({duration_ms / 60000:.1f}分)")
                continue

            location = visit.get("location", {})
            lat = location.get("latitudeE7", 0) / 1e7
            lon = location.get("longitudeE7", 0) / 1e7
            place_coords = (lat, lon)

            if self.home_coordinates and great_circle(place_coords, self.home_coordinates).meters < self.exclude_radius_meters:
                logging.debug(f"[{i}] スキップ: 自宅の範囲内です。")
                continue
            if self.work_coordinates and great_circle(place_coords, self.work_coordinates).meters < self.exclude_radius_meters:
                logging.debug(f"[{i}] スキップ: 勤務先の範囲内です。")
                continue

            end_time = datetime.fromtimestamp(end_ts_ms / 1000, tz=JST)
            date_str = start_time.strftime('%Y-%m-%d')
            
            place_name = location.get("name", "名称不明の場所")
            logging.info(f"✅ 発見: {date_str} {place_name} ({start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')})")
            
            if date_str not in visits_by_date:
                visits_by_date[date_str] = []

            visits_by_date[date_str].append({
                "name": place_name,
                "start": start_time.strftime('%H:%M'),
                "end": end_time.strftime('%H:%M')
            })
        
        if not visits_by_date:
            feedback = f"✅ 処理対象となる30分以上の滞在記録（自宅・勤務先以外）は見つかりませんでした。"
            if date_filter:
                feedback += f" (期間: {date_filter})"
            await message.reply(feedback)
        else:
            for date_str, visits in sorted(visits_by_date.items()):
                log_entries = [f"- {v['name']} ({v['start']} - {v['end']})" for v in visits]
                text_to_add = "\n".join(log_entries)
                await self._save_data_to_obsidian(date_str, text_to_add)
            
            start_date = min(visits_by_date.keys())
            end_date = max(visits_by_date.keys())
            await message.reply(f"✅ ロケーション履歴の記録が完了しました！ (対象期間: {start_date} ~ {end_date})")

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
                 return current_content.strip() + "\n" + new_section_with_header

            existing_sections = {line.strip(): i for i, line in enumerate(lines) if line.strip() in SECTION_ORDER}
            try:
                new_section_order_index = SECTION_ORDER.index(section_header)
            except ValueError:
                return current_content.strip() + "\n" + new_section_with_header

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

            return current_content.strip() + "\n" + new_section_with_header

    async def _save_data_to_obsidian(self, date_str: str, log_text: str):
        daily_note_path = f"{self.dropbox_vault_path}/DailyNotes/{date_str}.md"
        
        try:
            _, res = self.dbx.files_download(daily_note_path)
            current_content = res.content.decode('utf-8')
        except ApiError as e:
            if isinstance(e.error, DownloadError) and e.error.is_path() and e.error.get_path().is_not_found():
                current_content = ""
            else:
                raise

        new_daily_content = self._update_daily_note_with_ordered_section(
            current_content, log_text, "## Location Logs"
        )
        
        self.dbx.files_upload(
            new_daily_content.encode('utf-8'),
            daily_note_path,
            mode=WriteMode('overwrite')
        )
        logging.info(f"LocationLogCog: {daily_note_path} を更新しました。")


async def setup(bot: commands.Bot):
    """CogをBotに追加する"""
    await bot.add_cog(LocationLogCog(bot))