"""投資日記 (Journal) 関連エンドポイント。

routes.py から段階的に切り出した 2 つめのサブモジュール。
パターン: G-1 (investment_watchlist.py) と同様。
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.routes import verify_api_key, _get_investment_cog

router = APIRouter(prefix="/investment/journal", tags=["investment"])


class JournalAddRequest(BaseModel):
    title: Optional[str] = ""
    content: str
    ticker: Optional[str] = ""
    action: Optional[str] = ""   # buy / sell / hold / observe
    emotion: Optional[str] = ""


class JournalAnalyzeRequest(BaseModel):
    limit: int = 30


class JournalTitleRequest(BaseModel):
    content: str
    ticker: str = ""
    action: str = ""
    emotion: str = ""
    name: str = ""  # 銘柄名（あれば優先してタイトルに含める）


@router.get("", dependencies=[Depends(verify_api_key)])
async def investment_journal_list(limit: int = 50):
    cog = _get_investment_cog()
    return await cog.journal_list(limit=limit)


@router.post("/add", dependencies=[Depends(verify_api_key)])
async def investment_journal_add(req: JournalAddRequest):
    cog = _get_investment_cog()
    return await cog.journal_add(req.dict())


@router.get("/{filename}", dependencies=[Depends(verify_api_key)])
async def investment_journal_get(filename: str):
    cog = _get_investment_cog()
    return await cog.journal_get(filename)


@router.put("/{filename}", dependencies=[Depends(verify_api_key)])
async def investment_journal_edit(filename: str, req: JournalAddRequest):
    cog = _get_investment_cog()
    return await cog.journal_edit(filename, req.dict())


@router.delete("/{filename}", dependencies=[Depends(verify_api_key)])
async def investment_journal_delete(filename: str):
    cog = _get_investment_cog()
    return await cog.journal_delete(filename)


@router.post("/analyze", dependencies=[Depends(verify_api_key)])
async def investment_journal_analyze(req: JournalAnalyzeRequest):
    cog = _get_investment_cog()
    return await cog.journal_analyze_pattern(limit=req.limit)


@router.post("/suggest_title", dependencies=[Depends(verify_api_key)])
async def investment_journal_suggest_title(req: JournalTitleRequest):
    """投資日記の本文から、短いタイトル案を1つ生成する。
    銘柄ティッカーがあれば watchlist から名前を逆引きして、
    タイトル先頭に「【銘柄名(コード)】」を必ず含める。"""
    from api import app

    content = (req.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="本文が空です")

    bot = getattr(app.state, "bot", None)
    if not bot or not getattr(bot, "gemini_client", None):
        raise HTTPException(status_code=503, detail="Gemini未接続")

    # 銘柄名の決定: name > watchlist/portfolio から ticker で逆引き > ticker そのまま
    ticker = (req.ticker or "").strip()
    stock_label = (req.name or "").strip()
    if ticker and not stock_label:
        try:
            from api.database import watchlist_list
            wl = await watchlist_list()
            found = next((w for w in wl if (w.get("code") or "").upper() == ticker.upper()), None)
            if found and found.get("name"):
                stock_label = found["name"]
        except Exception as e:
            logging.debug(f"suggest_title watchlist lookup failed: {e}")
    if not stock_label:
        stock_label = ticker

    meta_parts = []
    if ticker:
        meta_parts.append(f"銘柄コード: {ticker}")
    if stock_label and stock_label != ticker:
        meta_parts.append(f"銘柄名: {stock_label}")
    if req.action:
        meta_parts.append(f"アクション: {req.action}")
    if req.emotion:
        meta_parts.append(f"感情: {req.emotion}")
    meta_str = " / ".join(meta_parts) or "（なし）"

    if stock_label and ticker and stock_label != ticker:
        stock_tag = f"【{stock_label}({ticker})】"
    elif ticker:
        stock_tag = f"【{ticker}】"
    else:
        stock_tag = ""

    rule_lines = [
        "次の投資日記の本文から、内容が一目で分かる簡潔な日本語のタイトルを1つだけ作ってください。",
        "・25文字以内。タイトルの文字列だけを返す（前置き・説明は不要）。",
    ]
    if stock_tag:
        rule_lines.append(
            f"・**必須**: タイトルの先頭に必ず `{stock_tag}` を付ける。"
            "この銘柄ラベルは省略・改変せずそのまま含めること。"
        )
        rule_lines.append(
            "・銘柄ラベルの後に半角スペースを1つ入れ、続けて本文の要約（15文字以内目安）を書く。"
        )
    else:
        rule_lines.append("・銘柄情報がない場合は内容の要約のみを書く。")

    prompt = (
        "\n".join(rule_lines)
        + f"\n【メタ情報】{meta_str}\n"
        + f"【本文】\n{content[:2000]}"
    )
    try:
        from services.gemini_model_resolver import resolve_gemini_model as _rgm
        _m = await _rgm("journal_title", default_pro=False)
        response = await bot.gemini_client.aio.models.generate_content(
            model=_m, contents=prompt,
        )
        title = (response.text or "").strip().strip("「」\"'").splitlines()[0][:40]
        return {"ok": True, "title": title}
    except Exception as e:
        logging.error(f"journal suggest_title error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
