"""Web Push 通知の送信を担当するサービス。

VAPID 鍵は環境変数から読み込み:
    VAPID_PUBLIC_KEY  - URL-safe Base64 エンコードされた公開鍵
    VAPID_PRIVATE_KEY - URL-safe Base64 エンコードされた秘密鍵
    VAPID_SUBJECT     - 連絡先 (mailto:... または https://...)

VAPID 鍵の生成は scratch/gen_vapid.py を実行して .env へコピーする。
"""

import os
import re
import json
import logging
import asyncio
import datetime
from typing import Optional

from config import JST

# AI が誤って返信冒頭に付けてしまう日時タグ（[今日 HH:MM] / [2026-05-20 HH:MM]）を除去する。
# これらのタグは履歴を読ませるための内部注釈であり、表示メッセージには不要。
_LEADING_TIME_TAG_RE = re.compile(
    r"^\s*\[(?:今日|昨日|\d{4}-\d{2}-\d{2})\s+\d{1,2}:\d{2}\]\s*"
)


def _strip_leading_time_tag(text: str) -> str:
    """メッセージ冒頭の日時タグを取り除く（複数連続していても除去）。"""
    if not text:
        return text
    prev = None
    while prev != text:
        prev = text
        text = _LEADING_TIME_TAG_RE.sub("", text, count=1)
    return text

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


async def send_push(
    title: str, body: str, url: Optional[str] = None,
    actions: Optional[list] = None, data: Optional[dict] = None,
) -> int:
    """全サブスクリプションへ通知を送信し、配信成功数を返す。
    送信不可（404/410）のサブスクリプションは自動で削除する。
    actions: 通知のアクションボタン [{"action": id, "title": ラベル}, ...]（最大2件程度）。
    data: 通知に同梱する追加データ（SW のクリック処理が参照。例: qid / answers）。"""
    if not is_configured():
        return 0

    subs = await get_all_push_subscriptions()
    if not subs:
        return 0

    preview = (body or "").replace("\n", " ")[:120]
    payload_obj = {"title": title, "body": preview, "url": url or "/"}
    if actions:
        payload_obj["actions"] = actions
    if data:
        payload_obj.update(data)
    payload = json.dumps(payload_obj, ensure_ascii=False)

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
_held_notifications: list[dict] = []     # 保留中の定時通知（古い順）。各要素は {content, title, created_at(datetime)}
_HELD_MAX = 30                           # 保留キューの上限（超過時は最古を破棄）
# しきい値: これより古い保留は「会話に紛れさせると不自然」なので
# proactive push で送り切る（または放出時に破棄する）。
_HELD_AUTO_FLUSH_MINUTES = 60            # 60 分以内に応答がなければ proactive push へ
_HELD_DISCARD_HOURS = 6                  # 6 時間超過した保留は放出時に破棄
_queue_lock = asyncio.Lock()


async def send_notice_batch(items: list[dict], push_title: str, push_url: str = "/") -> int:
    """複数のお知らせを「1件ずつ」個別の manager_notice（既読チェック可能なカード）として
    保存し、プッシュは「まとめて1発」だけ送る。

    items: [{"category": str, "title": str, "body": str}, ...]（body が空の項目はスキップ）
    返り値: 実際に保存した件数。
    """
    from api.database import add_manager_notice, save_message

    saved = 0
    saved_titles: list[str] = []
    for it in items:
        body = (it.get("body") or "").strip()
        if not body:
            continue
        try:
            title = it.get("title", "お知らせ")
            await add_manager_notice(it.get("category", ""), title, body)
            saved += 1
            saved_titles.append(title)
        except Exception as e:
            logging.error(f"send_notice_batch 保存エラー: {e}")

    if not saved:
        return 0

    # チャットにも導線を残す。プッシュを見逃しても、どんな項目が通知ログに
    # 入ったか一覧でき、[ACTION:open_notices] ボタンから1タップで開ける。
    # （プッシュは下で1発だけ送るので、ここではチャット保存のみ。）
    try:
        lead_lines = [f"📨 {push_title}を {saved} 件お届けしたよ。下のボタンから通知ログを見てね。"]
        for t in saved_titles:
            lead_lines.append(f"・{t}")
        lead_lines.append("[ACTION:open_notices]")
        await save_message("assistant", "\n".join(lead_lines))
    except Exception as e:
        logging.error(f"send_notice_batch チャット導線エラー: {e}")

    if is_configured():
        try:
            await send_push(push_title, f"{push_title}が {saved} 件届いたよ📨", url=push_url)
        except Exception as e:
            logging.error(f"send_notice_batch push エラー: {e}")
    return saved


async def _deliver(role: str, content: str, reply_to, title: str) -> int:
    """実際に DB へ保存し Push を送る（キューイング判定を行わない低レベル送信）。"""
    from api.database import save_message

    if role == "assistant" and content:
        content = _strip_leading_time_tag(content)

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

    # proactive 通知は保留せず、その場で送信する（ユーザー設定で「定時通知」モードに変更）。
    # かつて「未応答なら保留→次の発話で1件ずつ放出」というキューイングをしていたが、
    # 日をまたいでの文脈ズレ等の不都合があり廃止。マーク用フラグだけ管理する。
    if proactive and role == "assistant" and content and content.strip():
        async with _queue_lock:
            _awaiting_user_response = True

    return await _deliver(role, content, reply_to, title)


async def flush_stale_held_notifications() -> int:
    """保留キュー廃止後は常に 0 を返す（互換のため関数は残す）。"""
    async with _queue_lock:
        if not _held_notifications:
            return 0
        # 念のため残骸があれば即送出
        items = list(_held_notifications)
        _held_notifications.clear()
    for item in items:
        try:
            await _deliver("assistant", item["content"], None, item.get("title", "マネージャーからメッセージ"))
        except Exception:
            pass
    return len(items)


async def _legacy_flush_stale_held_notifications() -> int:
    """保留キューを走査し、`_HELD_AUTO_FLUSH_MINUTES` より古い項目を
    proactive push として一気に送り出す。

    背景: 夜に保留した「お疲れさま」のような通知を、ユーザーが翌朝にチャットを
    送るまで保持し続けると、朝に届いて文脈が破綻する。一定時間で会話の中に
    紛れさせるのを諦め、定時通知として送ってしまう方が自然。

    定期タスク（proactive_alert_cog の 15 分ループ等）から定期的に呼び出す。
    返り値は送出した件数。
    """
    global _awaiting_user_response
    now = datetime.datetime.now(JST)
    threshold = now - datetime.timedelta(minutes=_HELD_AUTO_FLUSH_MINUTES)

    to_flush: list[dict] = []
    async with _queue_lock:
        remaining: list[dict] = []
        for item in _held_notifications:
            ts = item.get("created_at") or now
            if ts <= threshold:
                to_flush.append(item)
            else:
                remaining.append(item)
        _held_notifications[:] = remaining
        # 全部捌けた場合は応答待ち状態を解除（会話再開時の余計な発射を避ける）
        if not _held_notifications and to_flush:
            _awaiting_user_response = False

    for item in to_flush:
        try:
            await _deliver("assistant", item["content"], None, item.get("title", "マネージャーからメッセージ"))
        except Exception as e:
            logging.error(f"stale 保留通知の放出に失敗: {e}")
    if to_flush:
        logging.info(f"古い保留通知を {len(to_flush)} 件 proactive 送出した")
    return len(to_flush)


async def mark_user_responded() -> None:
    """ユーザーがマネージャーへ反応したときに呼ぶ。
    保留キュー廃止後は実質 no-op。互換のため呼び出し側は変更しない。"""
    global _awaiting_user_response
    async with _queue_lock:
        _awaiting_user_response = False
