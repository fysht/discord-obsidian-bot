"""投資分析系エンドポイント（sentiment/snapshot/audit/earnings/ceo/peer/news/dividend/risk/constitution/history）。"""

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel

from api.routes import verify_api_key, _get_investment_cog

router = APIRouter(prefix="/investment", tags=["investment"])


class InvestmentTickerRequest(BaseModel):
    ticker: str


class InvestmentEarningsRequest(BaseModel):
    ticker: str
    register_calendar: bool = True


class InvestmentCEORequest(BaseModel):
    ticker: str
    video_url: str
    video_title: Optional[str] = ""


class InvestmentConstitutionUpdateRequest(BaseModel):
    content: str


class InvestmentConstitutionInitRequest(BaseModel):
    force: bool = False


class InvestmentEarningsDocSaveUrlRequest(BaseModel):
    ticker: str
    url: str
    label: str = ""


class InvestmentDividendRequest(BaseModel):
    ticker: str
    register_calendar: bool = True


class InvestmentReviewRequest(BaseModel):
    lookback_days: int = 180


@router.post("/sentiment", dependencies=[Depends(verify_api_key)])
async def investment_sentiment():
    cog = _get_investment_cog()
    return await cog.run_market_sentiment()


@router.post("/snapshot", dependencies=[Depends(verify_api_key)])
async def investment_snapshot(req: InvestmentTickerRequest):
    cog = _get_investment_cog()
    return await cog.run_stock_snapshot(req.ticker)


@router.post("/audit", dependencies=[Depends(verify_api_key)])
async def investment_audit(req: InvestmentTickerRequest):
    cog = _get_investment_cog()
    return await cog.run_stock_audit(req.ticker)


@router.post("/earnings_schedule", dependencies=[Depends(verify_api_key)])
async def investment_earnings_schedule(req: InvestmentEarningsRequest):
    cog = _get_investment_cog()
    return await cog.run_earnings_schedule(req.ticker, register_calendar=req.register_calendar)


@router.post("/earnings_documents", dependencies=[Depends(verify_api_key)])
async def investment_earnings_documents(req: InvestmentTickerRequest):
    cog = _get_investment_cog()
    return await cog.run_earnings_documents(req.ticker)


@router.post("/earnings_documents/save_url", dependencies=[Depends(verify_api_key)])
async def investment_earnings_documents_save_url(req: InvestmentEarningsDocSaveUrlRequest):
    cog = _get_investment_cog()
    return await cog.save_earnings_document_from_url(req.ticker, req.url, label=req.label)


@router.post("/earnings_documents/save_file", dependencies=[Depends(verify_api_key)])
async def investment_earnings_documents_save_file(
    ticker: str = Form(...),
    label: str = Form(""),
    file: UploadFile = File(...),
):
    cog = _get_investment_cog()
    content = await file.read()
    mime = file.content_type or ""
    return await cog.save_earnings_document_from_bytes(
        ticker, content, filename=file.filename or "document", label=label, mime=mime
    )


@router.post("/ceo_check", dependencies=[Depends(verify_api_key)])
async def investment_ceo_check(req: InvestmentCEORequest):
    cog = _get_investment_cog()
    return await cog.run_ceo_crosscheck(
        req.ticker, req.video_url, video_title=req.video_title or ""
    )


@router.get("/constitution", dependencies=[Depends(verify_api_key)])
async def investment_constitution_get():
    cog = _get_investment_cog()
    return await cog.run_get_constitution()


@router.post("/constitution", dependencies=[Depends(verify_api_key)])
async def investment_constitution_update(req: InvestmentConstitutionUpdateRequest):
    cog = _get_investment_cog()
    return await cog.run_update_constitution(req.content)


@router.post("/constitution/init", dependencies=[Depends(verify_api_key)])
async def investment_constitution_init(req: InvestmentConstitutionInitRequest):
    cog = _get_investment_cog()
    return await cog.run_init_constitution(force=req.force)


@router.get("/history/{category}", dependencies=[Depends(verify_api_key)])
async def investment_history(category: str, limit: int = 20):
    cog = _get_investment_cog()
    return await cog.list_history(category, limit=limit)


@router.get("/history/{category}/{file_id}", dependencies=[Depends(verify_api_key)])
async def investment_history_item(category: str, file_id: str):
    cog = _get_investment_cog()
    return await cog.read_history_item(category, file_id)


@router.post("/peer_comparison", dependencies=[Depends(verify_api_key)])
async def investment_peer_comparison(req: InvestmentTickerRequest):
    cog = _get_investment_cog()
    return await cog.run_peer_comparison(req.ticker)


@router.post("/news_sentiment", dependencies=[Depends(verify_api_key)])
async def investment_news_sentiment(req: InvestmentTickerRequest):
    cog = _get_investment_cog()
    return await cog.run_news_sentiment(req.ticker)


@router.post("/dividend", dependencies=[Depends(verify_api_key)])
async def investment_dividend(req: InvestmentDividendRequest):
    cog = _get_investment_cog()
    return await cog.run_dividend_schedule(
        req.ticker, register_calendar=req.register_calendar
    )


@router.post("/risk_assessment", dependencies=[Depends(verify_api_key)])
async def investment_risk():
    cog = _get_investment_cog()
    return await cog.run_risk_assessment()


@router.post("/constitution_review", dependencies=[Depends(verify_api_key)])
async def investment_constitution_review(req: InvestmentReviewRequest):
    cog = _get_investment_cog()
    return await cog.run_constitution_review(lookback_days=req.lookback_days)
