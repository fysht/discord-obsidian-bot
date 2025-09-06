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

# --- 定数定義 ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
# 順序定義に "## Location Logs" を追加
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
        if not all([self.location_log_channel_id, self.dropbox_refresh_token]):
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
        """ファイルアップロードを監視し、JSONファイルが投稿されたら処理を開始する"""
        if not self.is_ready or message.author.bot or message.channel.id != self.location_log_channel_id:
            return

        if message.attachments:
            attachment = message.attachments[0]
            if attachment.filename.endswith('.json'):
                await message.add_reaction("⏳")
                try:
                    json_data = await self._download_and_parse_json(attachment)
                    if json_data:
                        await self._process_location_history(json_data, message)
                except Exception as e:
                    logging.error(f"ロケーション履歴の処理中にエラーが発生: {e}", exc_info=True)
                    await message.reply(f"❌ エラーが発生しました: {e}")
                    await message.remove_reaction("⏳", self.bot.user)
                    await message.add_reaction("❌")

    async def _download_and_parse_json(self, attachment: discord.Attachment) -> dict:
        """添付ファイルをダウンロードしてJSONとして解析する"""
        content_bytes = await attachment.read()
        return json.loads(content_bytes.decode('utf-8'))

    async def _process_location_history(self, data: dict, message: discord.Message):
        """ロケーション履歴データを処理し、Obsidianに保存するメインロジック"""
        timeline_objects = data.get("timelineObjects", [])
        visits_by_date = {}

        for obj in timeline_objects:
            if "placeVisit" in obj:
                visit = obj["placeVisit"]
                duration_ms = int(visit["duration"]["endTimestampMs"]) - int(visit["duration"]["startTimestampMs"])
                
                # 滞在時間が30分未満の場合はスキップ
                if duration_ms < 30 * 60 * 1000:
                    continue

                location = visit.get("location", {})
                lat = location.get("latitudeE7", 0) / 1e7
                lon = location.get("longitudeE7", 0) / 1e7
                place_coords = (lat, lon)

                # 除外リストのチェック
                if self.home_coordinates and great_circle(place_coords, self.home_coordinates).meters < self.exclude_radius_meters:
                    continue
                if self.work_coordinates and great_circle(place_coords, self.work_coordinates).meters < self.exclude_radius_meters:
                    continue

                start_time = datetime.fromtimestamp(int(visit["duration"]["startTimestampMs"]) / 1000, tz=JST)
                end_time = datetime.fromtimestamp(int(visit["duration"]["endTimestampMs"]) / 1000, tz=JST)
                
                date_str = start_time.strftime('%Y-%m-%d')
                
                if date_str not in visits_by_date:
                    visits_by_date[date_str] = []

                visits_by_date[date_str].append({
                    "name": location.get("name", "名称不明の場所"),
                    "start": start_time.strftime('%H:%M'),
                    "end": end_time.strftime('%H:%M')
                })
        
        if not visits_by_date:
            await message.reply("✅ 処理対象となる30分以上の滞在記録（自宅・勤務先以外）は見つかりませんでした。")
            await message.remove_reaction("⏳", self.bot.user)
            await message.add_reaction("✅")
            return

        # 日付ごとにデイリーノートを更新
        for date_str, visits in sorted(visits_by_date.items()):
            log_entries = [f"- {v['name']} ({v['start']} - {v['end']})" for v in visits]
            text_to_add = "\n".join(log_entries)
            await self._save_data_to_obsidian(date_str, text_to_add)
        
        # フィードバック
        start_date = min(visits_by_date.keys())
        end_date = max(visits_by_date.keys())
        await message.reply(f"✅ ロケーション履歴の記録が完了しました！ (対象期間: {start_date} ~ {end_date})")
        await message.remove_reaction("⏳", self.bot.user)
        await message.add_reaction("✅")
    
    def _update_daily_note_with_ordered_section(self, current_content: str, text_to_add: str, section_header: str) -> str:
        """fitbit_cog.pyから流用した、順序を維持してセクションを追加/更新する関数"""
        lines = current_content.split('\n')
        
        try:
            header_index = lines.index(section_header)
            # 既存セクションの内容を新しい内容で置き換える（ヘッダーはそのまま）
            end_index = header_index + 1
            while end_index < len(lines) and not lines[end_index].strip().startswith('## '):
                end_index += 1
            del lines[header_index + 1 : end_index]
            lines.insert(header_index + 1, text_to_add)
            return "\n".join(lines)
        except ValueError:
            # セクションが存在しない場合、正しい位置に新規作成
            new_section_with_header = f"\n{section_header}\n{text_to_add}"
            if not any(s in current_content for s in SECTION_ORDER):
                 return current_content.strip() + "\n" + new_section_with_header

            existing_sections = {line.strip(): i for i, line in enumerate(lines) if line.strip() in SECTION_ORDER}
            try:
                new_section_order_index = SECTION_ORDER.index(section_header)
            except ValueError:
                # SECTION_ORDERにないヘッダーの場合は末尾に追加
                return current_content.strip() + "\n" + new_section_with_header

            # 挿入すべき位置を後ろから探す
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

            # 挿入すべき位置を前から探す
            insert_before_index = -1
            for i in range(new_section_order_index + 1, len(SECTION_ORDER)):
                following_header = SECTION_ORDER[i]
                if following_header in existing_sections:
                    insert_before_index = existing_sections[following_header]
                    break
            
            if insert_before_index != -1:
                lines.insert(insert_before_index, new_section_with_header + "\n")
                return "\n".join(lines).strip()

            # どのセクションも見つからなければ末尾に追加
            return current_content.strip() + "\n" + new_section_with_header

    async def _save_data_to_obsidian(self, date_str: str, log_text: str):
        """取得したデータをObsidianのデイリーノートに保存する"""
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