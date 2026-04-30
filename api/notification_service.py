"""Web Push 通知の送信を担当するサービス。

VAPID 鍵は環境変数から読み込み:
    VAPID_PUBLIC_KEY  - URL-safe Base64 エンコードされた公開鍵
    VAPID_PRIVATE_KEY - URL-safe Base64 エンコードされた秘密鍵
    VAPID_SUBJECT     - 連絡先 (mailto:... または https://...)

VAPID 鍵の生成は scratch/gen_vapid.py を実行して .env へコピーする。
"""

import os
import json
import logging
import asyncio
from typing import Optional

from pywebpush import webpush, WebPushException

from api.database import get_all_push_subscriptions, remove_push_subscription


_VAPID_PUBLIC = os.getenv("VAPID_PUBLIC_KEY", "")
_VAPID_PRIVATE = os.getenv("VAPID_PRIVATE_KEY", "")
_VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "")


def is_configured() -> bool:
    return bool(_VAPID_PUBLIC and _VAPID_PRIVATE and _VAPID_SUBJECT)


def get_public_key() -> str:
    return _VAPID_PUBLIC


def _send_one(subscription: dict, payload: str) -> tuple[bool, Optional[int]]:
    """1 件の購読へ送信。成功なら (True, None)、失敗なら (False, status_code)。
    status_code が 404/410 のときは購読が無効化されている。"""
    try:
        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {
                    "p256dh": subscription["p256dh"],
                    "auth": subscription["auth"],
                },
            },
            data=payload,
            vapid_private_key=_VAPID_PRIVATE,
            vapid_claims={"sub": _VAPID_SUBJECT},
        )
        return True, None
    except WebPushException as e:
        status = getattr(e.response, "status_code", None) if e.response is not None else None
        return False, status
    except Exception as e:
        logging.error(f"WebPush 不明エラー: {e}")
        return False, None


async def send_push(title: str, body: str, url: Optional[str] = None) -> int:
    """全サブスクリプションへ通知を送信し、配信成功数を返す。
    送信不可（404/410）のサブスクリプションは自動で削除する。"""
    if not is_configured():
        return 0

    subs = await get_all_push_subscriptions()
    if not subs:
        return 0

    preview = (body or "").replace("\n", " ")[:120]
    payload = json.dumps({"title": title, "body": preview, "url": url or "/"})

    loop = asyncio.get_running_loop()
    success = 0
    dead_endpoints: list[str] = []

    for sub in subs:
        ok, status = await loop.run_in_executor(None, _send_one, sub, payload)
        if ok:
            success += 1
        elif status in (404, 410):
            dead_endpoints.append(sub["endpoint"])
        else:
            logging.warning(f"WebPush 配信失敗 (status={status}) endpoint={sub['endpoint'][:40]}...")

    for endpoint in dead_endpoints:
        await remove_push_subscription(endpoint)
        logging.info(f"無効な購読を削除: {endpoint[:40]}...")

    return success


async def save_message_and_notify(role: str, content: str, reply_to=None, title: str = "マネージャーからメッセージ") -> int:
    """assistant メッセージは保存と同時に Web Push 通知を送る共通ヘルパー。
    user 発言など通知が不要な場合は通常の save_message を直接使うこと。"""
    from api.database import save_message

    msg_id = await save_message(role, content, reply_to=reply_to)
    if role == "assistant" and content and content.strip():
        try:
            await send_push(title, content)
        except Exception as e:
            logging.error(f"Push 通知送信エラー: {e}")
    return msg_id
