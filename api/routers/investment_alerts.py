"""投資アラート（Alerts）関連エンドポイント。routes.py から切り出し。"""

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.routes import verify_api_key, _get_investment_cog

router = APIRouter(prefix="/investment/alerts", tags=["investment"])


class AlertAddRequest(BaseModel):
    ticker: Optional[str] = ""
    type: str  # per_below / per_above / price_below / price_above / drop_pct / rise_pct / earnings_within_days
    threshold: float
    enabled: bool = True
    memo: Optional[str] = ""


class AlertToggleRequest(BaseModel):
    rule_id: int
    enabled: bool


class AlertRemoveRequest(BaseModel):
    rule_id: int


@router.get("", dependencies=[Depends(verify_api_key)])
async def investment_alerts_list():
    cog = _get_investment_cog()
    return await cog.alerts_list()


@router.post("/add", dependencies=[Depends(verify_api_key)])
async def investment_alerts_add(req: AlertAddRequest):
    cog = _get_investment_cog()
    return await cog.alerts_add(req.dict())


@router.post("/toggle", dependencies=[Depends(verify_api_key)])
async def investment_alerts_toggle(req: AlertToggleRequest):
    cog = _get_investment_cog()
    return await cog.alerts_toggle(req.rule_id, req.enabled)


@router.post("/remove", dependencies=[Depends(verify_api_key)])
async def investment_alerts_remove(req: AlertRemoveRequest):
    cog = _get_investment_cog()
    return await cog.alerts_remove(req.rule_id)


@router.post("/check", dependencies=[Depends(verify_api_key)])
async def investment_alerts_check():
    cog = _get_investment_cog()
    return await cog.alerts_check_now()
