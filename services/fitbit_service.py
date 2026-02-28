import os
import json
import base64
import datetime
import logging
import asyncio
import re
import aiohttp

from config import JST, BOT_FOLDER

TOKEN_FILE_NAME = "fitbit_token.json"

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
        except Exception as e:
            logging.error(f"Fitbit Token Load Error: {e}")

    async def _save_token(self, new_refresh_token):
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
        await self._load_token()
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
                    return False
        except Exception as e:
            return False

    def _calculate_sleep_score(self, total_asleep_min, total_in_bed_min, deep_min, rem_min, wake_min) -> int:
        if total_asleep_min == 0: return 0
        duration_score = min(50, (total_asleep_min / 480) * 50)
        deep_percentage = (deep_min / total_asleep_min) * 100
        rem_percentage = (rem_min / total_asleep_min) * 100
        deep_score = 12.5 if deep_percentage >= 20 else 10 if deep_percentage >= 15 else 7.5 if deep_percentage >= 10 else 5
        rem_score = 12.5 if rem_percentage >= 25 else 10 if rem_percentage >= 20 else 7.5 if rem_percentage >= 15 else 5
        quality_score = deep_score + rem_score
        restlessness_percentage = (wake_min / total_in_bed_min) * 100 if total_in_bed_min > 0 else 100
        restoration_score = 25 if restlessness_percentage <= 5 else 22 if restlessness_percentage <= 10 else 18 if restlessness_percentage <= 15 else 14 if restlessness_percentage <= 20 else 10
        return min(100, round(duration_score + quality_score + restoration_score))

    async def get_stats(self, date_obj):
        if not await self._refresh_access_token(): return None
        date_str = date_obj.strftime("%Y-%m-%d")
        session = await self._get_session()
        headers = {"Authorization": f"Bearer {self.access_token}"}
        stats = {}
        
        # 1. Activityデータの取得
        try:
            url = f"https://api.fitbit.com/1/user/{self.user_id}/activities/date/{date_str}.json"
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    s = data.get('summary', {})
                    stats['steps'] = s.get('steps', 0)
                    stats['calories_out'] = s.get('caloriesOut', 0)
                    stats['resting_heart_rate'] = s.get('restingHeartRate', 'N/A')
                    stats['distance_km'] = next((d['distance'] for d in s.get('distances', []) if d['activity'] == 'total'), 0)
                    stats['active_minutes_very'] = s.get('veryActiveMinutes', 0)
                    stats['active_minutes_fairly'] = s.get('fairlyActiveMinutes', 0)
        except Exception as e:
            logging.error(f"Fitbit Activity Error: {e}")

        # 2. Sleepデータの取得
        try:
            url = f"https://api.fitbit.com/1.2/user/{self.user_id}/sleep/date/{date_str}.json"
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    sleep_log = data.get('sleep', [])
                    if sleep_log:
                        main_sleep = max(sleep_log, key=lambda x: x.get('minutesAsleep', 0))
                        total_asleep = main_sleep.get('minutesAsleep', 0)
                        total_in_bed = main_sleep.get('timeInBed', 0)
                        stats['total_sleep_minutes'] = total_asleep
                        stats['time_in_bed_minutes'] = total_in_bed
                        
                        levels = main_sleep.get('levels', {}).get('summary', {})
                        deep_min = levels.get('deep', {}).get('minutes', 0)
                        rem_min = levels.get('rem', {}).get('minutes', 0)
                        light_min = levels.get('light', {}).get('minutes', 0)
                        wake_min = levels.get('wake', {}).get('minutes', 0)
                        
                        stats['deep_sleep_minutes'] = deep_min
                        stats['rem_sleep_minutes'] = rem_min
                        stats['light_sleep_minutes'] = light_min
                        
                        stats['sleep_score'] = self._calculate_sleep_score(total_asleep, total_in_bed, deep_min, rem_min, wake_min)
        except Exception as e:
            logging.error(f"Fitbit Sleep Error: {e}")

        return stats

    def _update_frontmatter(self, content, stats):
        frontmatter_pattern = r"^---\n(.*?)\n---"
        match = re.search(frontmatter_pattern, content, re.DOTALL)
        
        fm_map = {}
        body = content
        if match:
            fm_content = match.group(1)
            body = content[match.end():]
            for line in fm_content.split('\n'):
                if ':' in line:
                    key, val = line.split(':', 1)
                    fm_map[key.strip()] = val.strip()
        else:
            fm_map['date'] = datetime.datetime.now(JST).strftime('%Y-%m-%d')
            
        update_keys = [
            'sleep_score', 'total_sleep_minutes', 'time_in_bed_minutes',
            'deep_sleep_minutes', 'rem_sleep_minutes', 'light_sleep_minutes',
            'steps', 'distance_km', 'calories_out', 'resting_heart_rate',
            'active_minutes_very', 'active_minutes_fairly'
        ]
        
        for k in update_keys:
            if k in stats and stats[k] != 'N/A' and stats[k] is not None:
                fm_map[k] = stats[k]

        new_fm_lines = [f"{k}: {v}" for k, v in fm_map.items()]
        
        section_header = "## 📊 Health Stats"
        stats_md = "\n".join([f"- **{k}**: {v}" for k, v in stats.items()])
        
        if section_header not in body:
             body += f"\n\n{section_header}\n{stats_md}"
        
        return f"---\n" + "\n".join(new_fm_lines) + "\n---\n" + body.lstrip()

    async def update_daily_note_with_stats(self, date_obj, stats):
        service = self.drive_service.get_service()
        date_str = date_obj.strftime("%Y-%m-%d")
        
        daily_folder = await self.drive_service.find_file(service, self.drive_service.folder_id, "DailyNotes")
        if not daily_folder:
            daily_folder = await self.drive_service.create_folder(service, self.drive_service.folder_id, "DailyNotes")
            
        f_id = await self.drive_service.find_file(service, daily_folder, f"{date_str}.md")
        
        content = f"# Daily Note {date_str}\n"
        if f_id:
            content = await self.drive_service.read_text_file(service, f_id)
        
        new_content = self._update_frontmatter(content, stats)
        
        if f_id:
            await self.drive_service.update_text(service, f_id, new_content)
        else:
            await self.drive_service.upload_text(service, daily_folder, f"{date_str}.md", new_content)
        
        return True