import os
import asyncio
import logging
from datetime import date
from typing import Optional, Dict, Any
import aiohttp
import base64
import dropbox
from dropbox.exceptions import ApiError
from dropbox.files import WriteMode

class FitbitClient:
    """
    Fitbit APIとの通信を管理し、アクセストークンの更新を自動的に行うクライアント
    """
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, dbx: dropbox.Dropbox, user_id: str = "-"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.initial_refresh_token = refresh_token # 環境変数からの初期トークン
        self.user_id = user_id
        self.session = aiohttp.ClientSession()
        self.lock = asyncio.Lock()
        
        self.dbx = dbx
        
        self.vault_path = os.getenv("DROPBOX_VAULT_PATH", "/ObsidianVault")
        self.token_path = f"{self.vault_path}/.bot/fitbit_refresh_token.txt"

    async def _get_latest_refresh_token(self) -> str:
        """Dropboxから最新のリフレッシュトークンを読み込む。なければ環境変数の初期値を使う"""
        try:
            _, res = self.dbx.files_download(self.token_path)
            token = res.content.decode('utf-8').strip()
            logging.info("Dropboxから最新のFitbitリフレッシュトークンを読み込みました。")
            return token
        except ApiError as e:
            if e.error.is_path() and e.error.get_path().is_not_found():
                logging.warning("Dropboxにトークンファイルが見つかりません。環境変数の初期トークンを使用します。")
                return self.initial_refresh_token
            else:
                logging.error(f"Dropboxからのトークン読み込みに失敗: {e}")
                return self.initial_refresh_token

    def _save_new_refresh_token(self, new_token: str):
        """新しいリフレッシュトークンをDropboxに保存して永続化する"""
        try:
            self.dbx.files_upload(
                new_token.encode('utf-8'),
                self.token_path,
                mode=WriteMode('overwrite')
            )
            logging.info(f"新しいFitbitリフレッシュトークンをDropboxに保存しました: {self.token_path}")
        except Exception as e:
            logging.error(f"Dropboxへのトークン保存に失敗しました: {e}", exc_info=True)

    async def _get_access_token(self) -> Optional[str]:
        """リフレッシュトークンを使って新しいアクセストークンを取得する"""
        url = "https://api.fitbit.com/oauth2/token"
        auth_header = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        headers = {
            "Authorization": f"Basic {auth_header}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        current_refresh_token = await self._get_latest_refresh_token()
        
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": current_refresh_token
        }
        
        async with self.session.post(url, headers=headers, data=payload) as resp:
            response_json = await resp.json()
            
            if resp.status == 200:
                logging.info("アクセストークンの取得に成功しました。")
                access_token = response_json.get("access_token")
                new_refresh_token = response_json.get("refresh_token")
                self._save_new_refresh_token(new_refresh_token)
                return access_token
            else:
                logging.error(f"トークン交換APIからのエラー (ステータス: {resp.status}): {response_json}")
                return None

    async def get_sleep_data(self, target_date: date) -> Optional[Dict[str, Any]]:
        access_token = await self._get_access_token()
        if not access_token: return None
        date_str = target_date.strftime('%Y-%m-%d')
        url = f"https://api.fitbit.com/1.2/user/{self.user_id}/sleep/date/{date_str}.json"
        headers = {"Authorization": f"Bearer {access_token}"}
        
        async with self.session.get(url, headers=headers) as resp:
            response_json = await resp.json()
            if resp.status == 200 and response_json.get('sleep'):
                logging.info(f"{target_date} の睡眠データを正常に取得しました。")
                
                # isMainがtrueのものを探す
                for sleep_log in response_json['sleep']:
                    if sleep_log.get('isMain'):
                        return sleep_log
                
                # isMainがない場合、最も長い睡眠を返す (フォールバック)
                return max(response_json['sleep'], key=lambda x: x.get('minutesAsleep', 0))

            else:
                logging.error(f"睡眠データAPIからのエラー (ステータス: {resp.status}): {response_json}")
                return None

    async def get_activity_summary(self, target_date: date) -> Optional[Dict[str, Any]]:
        access_token = await self._get_access_token()
        if not access_token: return None
        date_str = target_date.strftime('%Y-%m-%d')
        url = f"https://api.fitbit.com/1/user/{self.user_id}/activities/date/{date_str}.json"
        headers = {"Authorization": f"Bearer {access_token}"}

        async with self.session.get(url, headers=headers) as resp:
            response_json = await resp.json()
            if resp.status == 200 and 'summary' in response_json:
                logging.info(f"{target_date} の活動概要データを正常に取得しました。")
                heart_rate_zones = response_json.get('summary', {}).get('heartRateZones', [])
                response_json['summary']['heartRateZones'] = {zone['name']: zone for zone in heart_rate_zones}
                return response_json
            else:
                logging.error(f"活動概要データAPIからのエラー (ステータス: {resp.status}): {response_json}")
                return None