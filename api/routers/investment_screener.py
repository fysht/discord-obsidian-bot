"""銘柄スクリーナー（Screener）関連エンドポイント。
ライブ実行（styles/universes/run/analyze/jobs/cross_filter）と
保存済み結果（runs CRUD）の両方を扱う。
"""

import math
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.routes import verify_api_key

router = APIRouter(prefix="/investment/screener", tags=["investment"])


def _get_screener_cog():
    from api import app
    bot = getattr(app.state, "bot", None)
    if not bot:
        raise HTTPException(status_code=503, detail="Botエンジンが初期化されていません。")
    cog = bot.get_cog("ScreenerCog")
    if not cog:
        raise HTTPException(status_code=503, detail="ScreenerCogがロードされていません。")
    return cog


def _json_sanitize(obj):
    """dict/list を再帰的に走査し、NaN/Inf を None に置換して JSON 互換にする。"""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_sanitize(v) for v in obj]
    return obj


class ScreenerRunRequest(BaseModel):
    styles: Optional[List[str]] = None
    style: Optional[str] = None  # backward compat
    universe: str = "topix500"
    top_n: int = 10
    min_market_cap_jpy: Optional[int] = None
    exclude_sectors: Optional[List[str]] = None
    filter_overrides: Optional[dict] = None
    combine_mode: str = "any"


class ScreenerAnalyzeRequest(BaseModel):
    styles: Optional[List[str]] = None
    style: Optional[str] = None  # backward compat
    candidates: List[dict]
    use_pro: bool = False


class ScreenerCrossFilterRequest(BaseModel):
    candidates: List[dict]
    secondary_style: str
    enabled_filters: Optional[List[str]] = None


class ScreenerRunSaveRequest(BaseModel):
    title: Optional[str] = ""
    styles: Optional[List[str]] = None
    combine_mode: Optional[str] = "any"
    universe: Optional[str] = ""
    applied_filters: Optional[dict] = None
    candidates: Optional[List[dict]] = None
    qualitative_report: Optional[str] = ""


@router.get("/styles", dependencies=[Depends(verify_api_key)])
async def screener_styles():
    cog = _get_screener_cog()
    return await cog.list_styles()


@router.get("/universes", dependencies=[Depends(verify_api_key)])
async def screener_universes():
    cog = _get_screener_cog()
    return await cog.list_universes()


@router.post("/run", dependencies=[Depends(verify_api_key)])
async def screener_run(req: ScreenerRunRequest):
    cog = _get_screener_cog()
    styles = req.styles or ([req.style] if req.style else [])
    if not styles:
        raise HTTPException(status_code=422, detail="styles または style を1つ以上指定してください")
    result = await cog.run_multi_screening(
        styles=styles,
        top_n=req.top_n,
        universe_name=req.universe,
        min_market_cap_jpy=req.min_market_cap_jpy,
        exclude_sectors=req.exclude_sectors,
        filter_overrides=req.filter_overrides,
        combine_mode=req.combine_mode,
    )
    return _json_sanitize(result)


@router.post("/run_async", dependencies=[Depends(verify_api_key)])
async def screener_run_async(req: ScreenerRunRequest):
    """機械スクリーニングをバックグラウンドで起動。job_id を返すので、
    /jobs/{job_id} で進捗をポーリングする。完了時に Push 通知が飛ぶ。"""
    cog = _get_screener_cog()
    styles = req.styles or ([req.style] if req.style else [])
    if not styles:
        raise HTTPException(status_code=422, detail="styles または style を1つ以上指定してください")
    return await cog.start_machine_screening(
        styles=styles,
        top_n=req.top_n,
        universe_name=req.universe,
        min_market_cap_jpy=req.min_market_cap_jpy,
        exclude_sectors=req.exclude_sectors,
        filter_overrides=req.filter_overrides,
        combine_mode=req.combine_mode,
    )


@router.post("/analyze", dependencies=[Depends(verify_api_key)])
async def screener_analyze(req: ScreenerAnalyzeRequest):
    cog = _get_screener_cog()
    styles = req.styles or ([req.style] if req.style else [])
    if not styles:
        raise HTTPException(status_code=422, detail="styles または style を1つ以上指定してください")
    return await cog.start_qualitative_analysis(
        styles=styles,
        candidates=req.candidates,
        use_pro=req.use_pro,
    )


@router.get("/jobs/{job_id}", dependencies=[Depends(verify_api_key)])
async def screener_job(job_id: str):
    cog = _get_screener_cog()
    return await cog.get_job_status(job_id)


@router.post("/cross_filter", dependencies=[Depends(verify_api_key)])
async def screener_cross_filter(req: ScreenerCrossFilterRequest):
    cog = _get_screener_cog()
    if not req.candidates:
        raise HTTPException(status_code=422, detail="candidates は1件以上必要です")
    if not req.secondary_style:
        raise HTTPException(status_code=422, detail="secondary_style を指定してください")
    return await cog.apply_secondary_style(
        candidates=req.candidates,
        secondary_style=req.secondary_style,
        enabled_filters=req.enabled_filters,
    )


# ----- 保存済みスクリーニング結果 (runs) -----

@router.post("/runs", dependencies=[Depends(verify_api_key)])
async def screener_runs_save(req: ScreenerRunSaveRequest):
    from api.database import screener_run_save
    run_id = await screener_run_save(
        title=req.title or "",
        styles=req.styles or [],
        combine_mode=req.combine_mode or "any",
        universe=req.universe or "",
        applied_filters=req.applied_filters or {},
        candidates=req.candidates or [],
        qualitative_report=req.qualitative_report or "",
    )
    return {"ok": True, "id": run_id}


@router.get("/runs", dependencies=[Depends(verify_api_key)])
async def screener_runs_list():
    from api.database import screener_run_list
    items = await screener_run_list()
    return {"ok": True, "items": items}


@router.get("/runs/{run_id}", dependencies=[Depends(verify_api_key)])
async def screener_runs_get(run_id: int):
    from api.database import screener_run_get
    data = await screener_run_get(run_id)
    if not data:
        raise HTTPException(status_code=404, detail="保存済み結果が見つかりません")
    return {"ok": True, "data": data}


@router.delete("/runs/{run_id}", dependencies=[Depends(verify_api_key)])
async def screener_runs_delete(run_id: int):
    from api.database import screener_run_delete
    ok = await screener_run_delete(run_id)
    return {"ok": ok}
