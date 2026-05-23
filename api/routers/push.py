"""Web Push 通知（VAPID鍵公開・購読/解除・テスト）。"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import notification_service
from api.database import add_push_subscription, remove_push_subscription
from api.routes import verify_api_key

router = APIRouter(prefix="", tags=["push"])


class PushSubscriptionRequest(BaseModel):
    endpoint: str
    p256dh: str
    auth: str


class PushUnsubscribeRequest(BaseModel):
    endpoint: str


@router.get("/vapid_public_key")
async def vapid_public_key():
    """VAPID 公開鍵を返す。サブスクリプション時にフロントが SW に渡す。
    認証不要にしているのは未ログイン状態でも SW 登録時に取得したいため
    （秘密性は無く、漏れても問題ない値）。"""
    return {"key": notification_service.get_public_key(), "configured": notification_service.is_configured()}


@router.post("/push/subscribe", dependencies=[Depends(verify_api_key)])
async def push_subscribe(req: PushSubscriptionRequest):
    if not req.endpoint or not req.p256dh or not req.auth:
        raise HTTPException(status_code=400, detail="購読情報が不完全です。")
    await add_push_subscription(req.endpoint, req.p256dh, req.auth)
    return {"status": "success"}


@router.post("/push/unsubscribe", dependencies=[Depends(verify_api_key)])
async def push_unsubscribe(req: PushUnsubscribeRequest):
    await remove_push_subscription(req.endpoint)
    return {"status": "success"}


@router.post("/push/test", dependencies=[Depends(verify_api_key)])
async def push_test():
    """通知テスト送信。設定確認用。"""
    count = await notification_service.send_push("通知テスト", "通知が届けば設定はOKだよ！")
    return {"status": "success", "delivered": count}
