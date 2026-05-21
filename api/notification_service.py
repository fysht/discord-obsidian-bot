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


# ============================================================
# 定時通知のキューイング
#   ユーザーが直前のマネージャー通知に未応答のあいだは、後続の定時通知を
#   保留キューに溜めておき、ユーザーが反応（チャット送信）するたびに
#   1 件ずつ放出する。これにより「一方的に連投される」状態を防ぐ。
# ============================================================

_awaiting_user_response = False          # 直近の定時通知にユーザーが未応答か
_held_notifications: list[dict] = []     # 保留中の定時通知（古い順）
_HELD_MAX = 30                           # 保留キューの上限（超過時は最古を破棄）
_queue_lock = asyncio.Lock()


async def _deliver(role: str, content: str, reply_to, title: str) -> int:
    """実際に DB へ保存し Push を送る（キューイング判定を行わない低レベル送信）。"""
    from api.database import save_message

    msg_id = await save_message(role, content, reply_to=reply_to)
    if role == "assistant" and content and content.strip():
        try:
            await send_push(title, content)
        except Exception as e:
            logging.error(f"Push 通知送信エラー: {e}")
    return msg_id


async def save_message_and_notify(
    role: str,
    content: str,
    reply_to=None,
    title: str = "マネージャーからメッセージ",
    proactive: bool = False,
) -> Optional[int]:
    """assistant メッセージは保存と同時に Web Push 通知を送る共通ヘルパー。

    proactive=True（定時通知・先回りリマインドなど、ユーザー発言を起点と
    しないマネージャー通知）の場合、直前の通知にユーザーが未応答であれば
    保留キューへ積み、送信を見送って None を返す。
    user 発言など通知が不要な場合は通常の save_message を直接使うこと。
    """
    global _awaiting_user_response

    if proactive and role == "assistant" and content and content.strip():
        async with _queue_lock:
            if _awaiting_user_response:
                _held_notifications.append({"content": content, "title": title})
                if len(_held_notifications) > _HELD_MAX:
                    _held_notifications.pop(0)
                logging.info(
                    f"定時通知を保留（ユーザー未応答）。保留件数={len(_held_notifications)}"
                )
                return None
            # 未応答フラグが立っていない → 今回は送信し、以後は応答待ち状態にする
            _awaiting_user_response = True

    return await _deliver(role, content, reply_to, title)


async def mark_user_responded() -> None:
    """ユーザーがマネージャーへ反応（チャット送信など）したときに呼ぶ。

    保留キューに通知があれば最古の 1 件だけを放出する。残りは次の応答まで
    引き続き保留する（1 応答につき 1 通知のペースで小出しにする）。
    保留が無ければ応答待ち状態を解除する。
    """
    global _awaiting_user_response

    async with _queue_lock:
        if not _held_notifications:
            _awaiting_user_response = False
            return
        nxt = _held_notifications.pop(0)
        remaining = len(_held_notifications)
        # まだ保留が残る／今 1 件出すので、応答待ち状態は維持する
        _awaiting_user_response = True

    content = nxt["content"]
    try:
        await _deliver("assistant", content, None, nxt["title"])
    except Exception as e:
        logging.error(f"保留通知の放出に失敗: {e}")
    logging.info(f"保留通知を1件放出。残り保留={remaining}")
