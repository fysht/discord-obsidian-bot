"""コストメーター（API利用料金）関連エンドポイント。"""

import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.routes import verify_api_key
from config import JST

router = APIRouter(prefix="", tags=["cost"])


class CostSettingsRequest(BaseModel):
    usd_jpy_rate: Optional[float] = None
    monthly_threshold_jpy: Optional[float] = None
    auto_downgrade_to_flash: Optional[bool] = None
    infra_cost_jpy_per_month: Optional[float] = None


@router.get("/cost_summary", dependencies=[Depends(verify_api_key)])
async def cost_summary(days: int = 30):
    """直近 N 日分のコスト集計を返す。既定 30 日。"""
    from services import cost_meter_service
    days = max(1, min(int(days or 30), 365))
    end = datetime.datetime.now(JST).date()
    start = end - datetime.timedelta(days=days - 1)
    data = await cost_meter_service.summary(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))

    month_start = end.replace(day=1).strftime("%Y-%m-%d")
    this_month = await cost_meter_service.summary(month_start, end.strftime("%Y-%m-%d"))
    infra = await cost_meter_service.get_infra_cost_jpy()
    threshold = await cost_meter_service.get_monthly_threshold_jpy()
    return {
        **data,
        "this_month_jpy": this_month["total_jpy"],
        "this_month_in_tokens": this_month["total_in_tokens"],
        "this_month_out_tokens": this_month["total_out_tokens"],
        "infra_cost_jpy_per_month": infra,
        "monthly_threshold_jpy": threshold,
        "monthly_total_jpy_including_infra": this_month["total_jpy"] + infra,
    }


@router.get("/cost_settings", dependencies=[Depends(verify_api_key)])
async def cost_settings_get():
    from services import cost_meter_service
    return await cost_meter_service.get_settings()


@router.post("/cost_settings", dependencies=[Depends(verify_api_key)])
async def cost_settings_set(req: CostSettingsRequest):
    from services import cost_meter_service
    payload = {k: v for k, v in req.model_dump().items() if v is not None}
    return await cost_meter_service.update_settings(payload)
