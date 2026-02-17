import os
import asyncio
import logging
from datetime import date
import time
from typing import Optional, Dict, Any
import aiohttp
import base64
import io
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

class FitbitClient:
    def __init__(self, client_id: str, client_secret: str, drive_service, drive_folder_id: str, user_id: str = "-"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_id = user_id
        self.session = aiohttp.ClientSession()
        self.drive_service = drive_service
        self.drive_folder_id = drive_folder_id
        self.token_file_name = "fitbit_refresh_token.txt"

        # ★追加：トークンの使い回し（キャッシュ）と同時実行防止の仕組み
        self._cached_access_token = None
        self._token_expires_at = 0
        self._token_lock = asyncio.Lock()

    async def _find_file(self, parent_id, name):
        loop = asyncio.get_running_loop()
        try:
            res = await loop.run_in_executor(None, lambda: self.drive_service.files().list(q=f"'{parent_id}' in parents and name = '{name}' and trashed = false", fields="files(id)").execute())
            files = res.get('files', [])
            return files[0]['id'] if files else None
        except: return None

    async def _get_latest_refresh_token(self) -> str:
        f_id = await self._find_file(self.drive_folder_id, self.token_file_name)
        if f_id:
            try:
                loop = asyncio.get_running_loop()
                request = self.drive_service.files().get_media(fileId=f_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = await loop.run_in_executor(None, downloader.next_chunk)
                return fh.getvalue().decode('utf-8').strip()
            except Exception as e:
                logging.error(f"Driveからのトークン読み込みに失敗: {e}")
        return os.getenv("FITBIT_REFRESH_TOKEN")

    async def _save_new_refresh_token(self, new_token: str):
        loop = asyncio.get_running_loop()
        f_id = await self._find_file(self.drive_folder_id, self.token_file_name)
        media = MediaIoBaseUpload(io.BytesIO(new_token.encode('utf-8')), mimetype='text/plain')
        try:
            if f_id:
                await loop.run_in_executor(None, lambda: self.drive_service.files().update(fileId=f_id, media_body=media).execute())
            else:
                await loop.run_in_executor(None, lambda: self.drive_service.files().create(body={'name': self.token_file_name, 'parents': [self.drive_folder_id]}, media_body=media).execute())
        except Exception as e:
            logging.error(f"Driveへのトークン保存に失敗: {e}")

    async def _get_access_token(self) -> Optional[str]:
        # すでに有効なアクセストークンがあれば使い回す（有効期限内かチェック）
        if self._cached_access_token and time.time() < self._token_expires_at:
            return self._cached_access_token

        # トークン取得処理が複数同時に走らないように順番待ち（ロック）させる
        async with self._token_lock:
            # 待っている間に別の処理がトークンを取得してくれていたら、それを使う
            if self._cached_access_token and time.time() < self._token_expires_at:
                return self._cached_access_token

            url = "https://api.fitbit.com/oauth2/token"
            auth_header = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            headers = {"Authorization": f"Basic {auth_header}", "Content-Type": "application/x-www-form-urlencoded"}
            current_refresh_token = await self._get_latest_refresh_token()
            payload = {"grant_type": "refresh_token", "refresh_token": current_refresh_token}
            
            async with self.session.post(url, headers=headers, data=payload) as resp:
                response_json = await resp.json()
                if resp.status == 200:
                    # 成功したらアクセストークンをメモリに保存（余裕を持って7時間で破棄設定）
                    self._cached_access_token = response_json.get("access_token")
                    self._token_expires_at = time.time() + (7 * 3600)
                    
                    await self._save_new_refresh_token(response_json.get("refresh_token"))
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