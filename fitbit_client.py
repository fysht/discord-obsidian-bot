import os
import fitbit
import asyncio
from datetime import date
import logging
from typing import Optional, Dict, Any

class FitbitClient:
    """
    Fitbit APIとの通信を管理し、アクセストークンの更新を自動的に行うクライアント
    """
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, user_id: str = "-"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.user_id = user_id
        self.client = self._create_client()
        self.lock = asyncio.Lock()

    def _token_refresher(self, token: Dict[str, Any]):
        """
        Fitbit APIから新しいアクセストークンが発行された際に呼び出されるコールバック
        ここでは新しいリフレッシュトークンを保存する（今回は環境変数なので何もしない）
        """
        # NOTE: 本来は新しいリフレッシュトークンを永続化するが、今回は環境変数運用なので何もしない
        logging.info("Fitbitのアクセストークンが更新されました。")
        self.refresh_token = token['refresh_token']

    def _create_client(self) -> fitbit.Fitbit:
        """fitbitクライアントのインスタンスを作成する"""
        return fitbit.Fitbit(
            self.client_id,
            self.client_secret,
            oauth2=True,
            access_token=None,  # 初期はNoneでOK
            refresh_token=self.refresh_token,
            refresh_cb=self._token_refresher,
        )

    async def get_sleep_data(self, target_date: date) -> Optional[Dict[str, Any]]:
        """
        指定された日付の睡眠データを取得する
        API呼び出しは非同期で実行する
        """
        async with self.lock:
            try:
                loop = asyncio.get_running_loop()
                # fitbitライブラリは非同期ではないため、executorで実行する
                response = await loop.run_in_executor(
                    None,
                    lambda: self.client.get_sleep(target_date)
                )

                if response and 'summary' in response and response.get('sleep'):
                    # 必要な情報だけを抽出して返す
                    main_sleep = response['sleep'][0] # 通常、最初のものがメインの睡眠
                    return {
                        "score": main_sleep.get("efficiency"), # API v1ではefficiencyがスコアに近い
                        "timeInBed": main_sleep.get("timeInBed"),
                        "minutesAsleep": main_sleep.get("minutesAsleep"),
                        "efficiency": main_sleep.get("efficiency"),
                        "levels": main_sleep.get("levels", {}).get("summary")
                    }
                else:
                    logging.warning(f"Fitbit APIから {target_date} の有効な睡眠データが取得できませんでした。")
                    return None
            except Exception as e:
                logging.error(f"Fitbit APIからの睡眠データ取得中にエラーが発生: {e}", exc_info=True)
                return None