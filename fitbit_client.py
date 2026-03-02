import os
import asyncio
import logging
from datetime import date
import time
from typing import Optional, Dict, Any
import aiohttp
import base64

class FitbitClient:
    def __init__(self, client_id: str, client_secret: str, drive_service, drive_folder_id: str, user_id: str = "-"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_id = user_id
        self.session = aiohttp.ClientSession()
        self.drive_service = drive_service
        self.drive_folder_id = drive_folder_id
        self.token_file_name = "fitbit_refresh_token.txt"

        self._cached_access_token = None
        self._token_expires_at = 0
        self._token_lock = asyncio.Lock()

    async def _get_latest_refresh_token(self) -> str:
        # ★修正: drive_service の安全なラッパーメソッドを使用
        service = self.drive_service.get_service()
        if not service:
            return os.getenv("FITBIT_REFRESH_TOKEN", "")

        f_id = await self.drive_service.find_file(service, self.drive_folder_id, self.token_file_name)
        if f_id:
            try:
                # DriveServiceのメソッドを使ってテキストとして読み込む
                content = await self.drive_service.read_text_file(service, f_id)
                if content:
                    return content.strip()
            except Exception as e:
                logging.error(f"Driveからのトークン読み込みに失敗: {e}")
        
        # Driveから取得できなかった場合は環境変数から
        return os.getenv("FITBIT_REFRESH_TOKEN", "")

    async def _save_new_refresh_token(self, new_token: str):
        # ★修正: drive_service の安全なラッパーメソッドを使用
        service = self.drive_service.get_service()
        if not service:
            logging.error("Driveサービスがないため、新しいトークンを保存できません。")
            return

        f_id = await self.drive_service.find_file(service, self.drive_folder_id, self.token_file_name)
        try:
            if f_id:
                # 既存ファイルがあれば上書き更新
                await self.drive_service.update_text(service, f_id, new_token)
            else:
                # なければ新規作成
                await self.drive_service.upload_text(service, self.drive_folder_id, self.token_file_name, new_token)
            logging.info("🎉 新しいFitbitリフレッシュトークンをGoogle Driveに保存しました。")
        except Exception as e:
            logging.error(f"Driveへのトークン保存に失敗: {e}")

    async def _get_access_token(self) -> Optional[str]:
        if self._cached_access_token and time.time() < self._token_expires_at:
            return self._cached_access_token

        async with self._token_lock:
            if self._cached_access_token and time.time() < self._token_expires_at:
                return self._cached_access_token

            url = "https://api.fitbit.com/oauth2/token"
            auth_header = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            headers = {"Authorization": f"Basic {auth_header}", "Content-Type": "application/x-www-form-urlencoded"}
            
            current_refresh_token = await self._get_latest_refresh_token()
            if not current_refresh_token:
                logging.error("🚨 リフレッシュトークンが取得できませんでした。")
                return None

            payload = {"grant_type": "refresh_token", "refresh_token": current_refresh_token}
            
            async with self.session.post(url, headers=headers, data=payload) as resp:
                response_json = await resp.json()
                if resp.status == 200:
                    self._cached_access_token = response_json.get("access_token")
                    self._token_expires_at = time.time() + (7 * 3600)
                    
                    # 成功したら直ちにGoogle Driveへ新しいリフレッシュトークンを保存
                    new_refresh_token = response_json.get("refresh_token")
                    if new_refresh_token:
                        await self._save_new_refresh_token(new_refresh_token)
                    
                    return self._cached_access_token
                else:
                    logging.error(f"🚨 トークン取得エラー ({resp.status}): {response_json}")
                    return None

    async def get_sleep_data(self, target_date: date) -> Optional[Dict[str, Any]]:
        access_token = await self._get_access_token()
        if not access_token: return None
        url = f"https://api.fitbit.com/1.2/user/{self.user_id}/sleep/date/{target_date.strftime('%Y-%m-%d')}.json"
        headers = {"Authorization": f"Bearer {access_token}"}
        async with self.session.get(url, headers=headers) as resp:
            response_json = await resp.json()
            if resp.status == 200 and response_json.get('sleep'): 
                return response_json
            else:
                logging.warning(f"⚠️ 睡眠データ取得失敗 ({resp.status}): {response_json}")
            return None

    async def get_activity_summary(self, target_date: date) -> Optional[Dict[str, Any]]:
        access_token = await self._get_access_token()
        if not access_token: return None
        url = f"https://api.fitbit.com/1/user/{self.user_id}/activities/date/{target_date.strftime('%Y-%m-%d')}.json"
        headers = {"Authorization": f"Bearer {access_token}"}
        async with self.session.get(url, headers=headers) as resp:
            response_json = await resp.json()
            if resp.status == 200 and 'summary' in response_json:
                heart_rate_zones = response_json.get('summary', {}).get('heartRateZones', [])
                response_json['summary']['heartRateZones'] = {zone['name']: zone for zone in heart_rate_zones}
                return response_json
            else:
                logging.warning(f"⚠️ 活動データ取得失敗 ({resp.status}): {response_json}")
            return None