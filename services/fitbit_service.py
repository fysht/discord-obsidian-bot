import os
import json
import base64
import datetime
import logging
import asyncio
import re
import aiohttp
import zoneinfo

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
TOKEN_FILE_NAME = "fitbit_token.json"
BOT_FOLDER = ".bot"

class FitbitService:
    def __init__(self, drive_service, client_id, client_secret, initial_refresh_token, user_id="-"):
        self.drive_service = drive_service
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = initial_refresh_token
        self.user_id = user_id
        self.access_token = None
        self.session = None

    async def _get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def _load_token(self):
        """Driveから最新のトークンを読み込む"""
        service = self.drive_service.get_service()
        if not service: return

        try:
            b_folder = await self.drive_service.find_file(service, self.drive_service.folder_id, BOT_FOLDER)
            if b_folder:
                f_id = await self.drive_service.find_file(service, b_folder, TOKEN_FILE_NAME)
                if f_id:
                    content = await self.drive_service.read_text_file(service, f_id)
                    data = json.loads(content)
                    self.refresh_token = data.get("refresh_token", self.refresh_token)
                    logging.info("FitbitService: Token loaded from Drive.")
        except Exception as e:
            logging.error(f"Fitbit Token Load Error: {e}")

    async def _save_token(self, new_refresh_token):
        """Driveにトークンを保存"""
        self.refresh_token = new_refresh_token
        service = self.drive_service.get_service()
        if not service: return

        try:
            b_folder = await self.drive_service.find_file(service, self.drive_service.folder_id, BOT_FOLDER)
            if not b_folder:
                b_folder = await self.drive_service.create_folder(service, self.drive_service.folder_id, BOT_FOLDER)
            
            data = json.dumps({"refresh_token": new_refresh_token}, indent=2)
            
            f_id = await self.drive_service.find_file(service, b_folder, TOKEN_FILE_NAME)
            if f_id:
                await self.drive_service.update_text(service, f_id, data, mime_type='application/json')
            else:
                await self.drive_service.upload_text(service, b_folder, TOKEN_FILE_NAME, data)
        except Exception as e:
            logging.error(f"Fitbit Token Save Error: {e}")

    async def _refresh_access_token(self):
        """アクセストークンを更新"""
        await self._load_token() # 最新のリフレッシュトークンを確認
        session = await self._get_session()
        
        url = "https://api.fitbit.com/oauth2/token"
        auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        headers = {
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token
        }

        try:
            async with session.post(url, headers=headers, data=data) as resp:
                if resp.status == 200:
                    resp_json = await resp.json()
                    self.access_token = resp_json["access_token"]
                    new_refresh = resp_json["refresh_token"]
                    await self._save_token(new_refresh)
                    return True
                else:
                    text = await resp.text()
                    logging.error(f"Fitbit Refresh Failed: {resp.status} {text}")
                    return False
        except Exception as e:
            logging.error(f"Fitbit Connection Error: {e}")
            return False

    async def get_stats(self, date_obj):
        """指定日のデータを取得（歩数、睡眠、心拍）"""
        if not await self._refresh_access_token():
            return None

        date_str = date_obj.strftime("%Y-%m-%d")
        session = await self._get_session()
        headers = {"Authorization": f"Bearer {self.access_token}"}
        
        stats = {}
        
        # 1. Activity (Steps, Calories)
        try:
            url = f"https://api.fitbit.com/1/user/{self.user_id}/activities/date/{date_str}.json"
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    s = data.get('summary', {})
                    stats['steps'] = s.get('steps', 0)
                    stats['calories'] = s.get('caloriesOut', 0)
                    stats['resting_hr'] = s.get('restingHeartRate', 'N/A')
        except Exception as e:
            logging.error(f"Fitbit Activity Error: {e}")

        # 2. Sleep
        try:
            url = f"https://api.fitbit.com/1.2/user/{self.user_id}/sleep/date/{date_str}.json"
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    sleep_log = data.get('sleep', [])
                    if sleep_log:
                        # 複数回の睡眠がある場合はメイン（一番長いもの）を採用、あるいは合計するロジック
                        main_sleep = max(sleep_log, key=lambda x: x.get('minutesAsleep', 0))
                        stats['sleep_minutes'] = main_sleep.get('minutesAsleep', 0)
                        stats['sleep_score'] = main_sleep.get('efficiency', 'N/A') # APIによってはscoreフィールドがある
                        # efficiencyで代用、もし 'efficiency' がない場合は自前計算ロジックを入れることも可能
        except Exception as e:
            logging.error(f"Fitbit Sleep Error: {e}")

        return stats

    def _update_frontmatter(self, content, stats):
        """Frontmatterを解析して更新・挿入する"""
        frontmatter_pattern = r"^---\n(.*?)\n---"
        match = re.search(frontmatter_pattern, content, re.DOTALL)
        
        new_fm_lines = []
        body = content

        if match:
            # 既存Frontmatterあり
            fm_content = match.group(1)
            body = content[match.end():]
            # 既存の行を保持しつつ、重複しないようにマップ化
            fm_map = {}
            for line in fm_content.split('\n'):
                if ':' in line:
                    key, val = line.split(':', 1)
                    fm_map[key.strip()] = val.strip()
            
            # 更新
            if 'steps' in stats: fm_map['steps'] = stats['steps']
            if 'calories' in stats: fm_map['calories'] = stats['calories']
            if 'sleep_time' in stats: fm_map['sleep_time'] = stats['sleep_minutes']
            if 'resting_hr' in stats: fm_map['resting_hr'] = stats['resting_hr']
            
            new_fm_lines = [f"{k}: {v}" for k, v in fm_map.items()]
        else:
            # Frontmatterなし
            new_fm_lines = [
                f"date: {datetime.datetime.now(JST).strftime('%Y-%m-%d')}",
            ]
            if 'steps' in stats: new_fm_lines.append(f"steps: {stats['steps']}")
            if 'calories' in stats: new_fm_lines.append(f"calories: {stats['calories']}")
            if 'sleep_time' in stats: new_fm_lines.append(f"sleep_time: {stats['sleep_minutes']}")
            if 'resting_hr' in stats: new_fm_lines.append(f"resting_hr: {stats['resting_hr']}")

        # Bodyへの追記（ヘルスセクション）
        section_header = "## Health Stats"
        stats_md = "\n".join([f"- **{k}**: {v}" for k, v in stats.items()])
        
        # 既存セクションがあれば置換、なければ追記
        # 簡易的な追記処理（utils.obsidian_utilsと同じロジックを利用）
        if section_header in body:
             # 既存セクションの更新は複雑なので、今回はシンプルに末尾追記or既存維持
             # utilsの update_section を使いたいが、ここでは文字列操作のみで行う
             pass 
        else:
             body += f"\n\n{section_header}\n{stats_md}"

        return f"---\n" + "\n".join(new_fm_lines) + "\n---\n" + body.lstrip()

    async def update_daily_note_with_stats(self, date_obj, stats):
        """Obsidianの日記を更新"""
        service = self.drive_service.get_service()
        date_str = date_obj.strftime("%Y-%m-%d")
        
        daily_folder = await self.drive_service.find_file(service, self.drive_service.folder_id, "DailyNotes")
        if not daily_folder:
            daily_folder = await self.drive_service.create_folder(service, self.drive_service.folder_id, "DailyNotes")
            
        f_id = await self.drive_service.find_file(service, daily_folder, f"{date_str}.md")
        
        content = f"# Daily Note {date_str}\n"
        if f_id:
            content = await self.drive_service.read_text_file(service, f_id)
        
        # Frontmatterと本文を更新
        new_content = self._update_frontmatter(content, stats)
        
        if f_id:
            await self.drive_service.update_text(service, f_id, new_content)
        else:
            await self.drive_service.upload_text(service, daily_folder, f"{date_str}.md", new_content)
        
        return True