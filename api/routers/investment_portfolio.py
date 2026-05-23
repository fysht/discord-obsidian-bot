"""保有銘柄（Portfolio）関連エンドポイント。routes.py から切り出し。"""

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.routes import verify_api_key, _get_investment_cog

router = APIRouter(prefix="/investment/portfolio", tags=["investment"])


class PortfolioAddRequest(BaseModel):
    ticker: str
    shares: float
    avg_cost: float
    name: Optional[str] = None
    sector: Optional[str] = None
    currency: Optional[str] = None
    notes: Optional[str] = None


class PortfolioRemoveRequest(BaseModel):
    code: str
    shares: Optional[float] = None  # None なら全数売却


class PortfolioEditRequest(BaseModel):
    code: str
    shares: Optional[float] = None
    avg_cost: Optional[float] = None
    name: Optional[str] = None
    sector: Optional[str] = None
    currency: Optional[str] = None
    notes: Optional[str] = None


@router.get("", dependencies=[Depends(verify_api_key)])
async def investment_portfolio_list():
    cog = _get_investment_cog()
    return await cog.portfolio_list()


@router.post("/add", dependencies=[Depends(verify_api_key)])
async def investment_portfolio_add(req: PortfolioAddRequest):
    cog = _get_investment_cog()
    return await cog.portfolio_add(req.dict())


@router.post("/remove", dependencies=[Depends(verify_api_key)])
async def investment_portfolio_remove(req: PortfolioRemoveRequest):
    cog = _get_investment_cog()
    return await cog.portfolio_remove(req.code, shares=req.shares)


@router.post("/edit", dependencies=[Depends(verify_api_key)])
async def investment_portfolio_edit(req: PortfolioEditRequest):
    cog = _get_investment_cog()
    payload = req.dict(exclude_none=True)
    code = payload.pop("code")
    return await cog.portfolio_update(code, **payload)


@router.get("/transactions", dependencies=[Depends(verify_api_key)])
async def investment_portfolio_transactions(limit: int = 100):
    cog = _get_investment_cog()
    return await cog.portfolio_transactions(limit=limit)
