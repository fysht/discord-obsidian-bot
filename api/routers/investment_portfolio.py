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
    opened_at: Optional[str] = None  # 実際の購入日(YYYY-MM-DD)。省略時は登録日。


class PortfolioRemoveRequest(BaseModel):
    code: str
    shares: Optional[float] = None  # None なら全数売却
    price: Optional[float] = None   # 実際の売却単価。実現損益の計算に使う（省略時は現値/平均取得単価）


class PortfolioEditRequest(BaseModel):
    code: str
    shares: Optional[float] = None
    avg_cost: Optional[float] = None
    name: Optional[str] = None
    sector: Optional[str] = None
    currency: Optional[str] = None
    notes: Optional[str] = None
    opened_at: Optional[str] = None  # 実際の購入日(YYYY-MM-DD)に補正できる。
    preferred_method: Optional[str] = None  # この銘柄が有利に見えるメソッド(style_name)


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
    return await cog.portfolio_remove(req.code, shares=req.shares, price=req.price)


@router.get("/realized", dependencies=[Depends(verify_api_key)])
async def investment_portfolio_realized():
    """売却で確定した実現損益（自分の売買がどれだけ利益を生んだか）を集計して返す。"""
    cog = _get_investment_cog()
    return await cog.portfolio_realized_summary()


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


@router.post("/review", dependencies=[Depends(verify_api_key)])
async def investment_portfolio_review():
    """保有銘柄をテクニカル×ファンダで診断し、継続/縮小/売却の昼チェック・レポートを返す。
    平日12時の自動通知と同じ内容を、その場で実行する（手動トリガー）。"""
    cog = _get_investment_cog()
    return await cog.run_holdings_review()


@router.post("/breakout_advise", dependencies=[Depends(verify_api_key)])
async def investment_portfolio_breakout_advise():
    """「じわじわ高値ブレイク」(topix500)で新規候補を抽出し、保有＋候補を一括診断したレポートを返す。
    平日16時(大引け後)の自動通知と同じ内容をその場で実行する（手動トリガー・約1〜3分）。"""
    cog = _get_investment_cog()
    return await cog.run_breakout_advise()
