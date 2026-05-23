"""注目銘柄（Watchlist）関連エンドポイント。

routes.py から段階的に切り出した最初のサブモジュール。
他の investment 系も同じパターンでここに集めていく予定。
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.routes import verify_api_key

router = APIRouter(prefix="/investment/watchlist", tags=["investment"])


class WatchlistAddRequest(BaseModel):
    code: str
    name: str = ""
    sector: str = ""
    source: str = ""
    memo: str = ""


class WatchlistMemoRequest(BaseModel):
    memo: str


@router.get("", dependencies=[Depends(verify_api_key)])
async def watchlist_get():
    from api.database import watchlist_list
    items = await watchlist_list()
    return {"ok": True, "items": items}


@router.post("", dependencies=[Depends(verify_api_key)])
async def watchlist_post(req: WatchlistAddRequest):
    from api.database import watchlist_add
    if not req.code:
        raise HTTPException(status_code=422, detail="code は必須です")
    await watchlist_add(req.code, req.name, req.sector, req.source, req.memo)
    return {"ok": True}


@router.delete("/{code}", dependencies=[Depends(verify_api_key)])
async def watchlist_delete(code: str):
    from api.database import watchlist_remove
    ok = await watchlist_remove(code)
    return {"ok": ok}


@router.put("/{code}/memo", dependencies=[Depends(verify_api_key)])
async def watchlist_memo(code: str, req: WatchlistMemoRequest):
    from api.database import watchlist_update_memo
    ok = await watchlist_update_memo(code, req.memo)
    return {"ok": ok}
