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


@router.get("/ohlcv/{code}", dependencies=[Depends(verify_api_key)])
async def screener_ohlcv(code: str, days: int = 120):
    """1 銘柄の OHLCV（分割調整済み）を返す。スクリーナー結果のチャート表示用。"""
    cog = _get_screener_cog()
    return _json_sanitize(await cog.get_ohlcv_series(code, days))


@router.get("/score/{code}", dependencies=[Depends(verify_api_key)])
async def screener_score(code: str, days: int = 300):
    """1銘柄を登録済みの全メソッド（テクニカル/ファンダ/複合）で採点し、メソッド別の
    魅力（点数）と一番有利なメソッドを返す。注目銘柄・保有銘柄の横断評価に使う。"""
    cog = _get_screener_cog()
    return _json_sanitize(await cog.score_all_methods(code, days))


@router.get("/projection/{code}", dependencies=[Depends(verify_api_key)])
async def screener_projection(code: str, days: int = 750):
    """1 銘柄の過去の高値ブレイク後の値動きから、上昇余地・利確目標・損切り目安を返す
    （決定論的・Gemini非依存）。じわじわ高値ブレイク等の候補の出口戦略づくりに使う。"""
    cog = _get_screener_cog()
    return _json_sanitize(await cog.analyze_projection(code, days))


class ScreenerAdviseRequest(BaseModel):
    candidates: Optional[List[dict]] = None  # 新規候補（スクリーニング結果など）
    holdings: Optional[List[dict]] = None    # 省略時は保有ポートフォリオを自動取得
    days: int = 300
    with_financials: bool = False            # EDINET有報の安全性/キャッシュ指標を織り込む
    capital: Optional[float] = None          # 総資金。与えると新規買い候補の建玉サイズを逆算
    hard_stop_pct: float = -0.08             # 保有銘柄のハード損切り（取得単価比、kenmo -8%/DUKE -10%）


@router.post("/advise", dependencies=[Depends(verify_api_key)])
async def screener_advise(req: ScreenerAdviseRequest):
    """保有銘柄と新規候補を、テクニカル(トレンド)×ファンダ(健全性)の二重視点で一括診断し、
    継続保有/縮小/売却・新規買い/見送り・銘柄入替の助言を返す（決定論的）。
    with_financials=True で EDINET 有報の自己資本比率・FCF・CF型も加味する。"""
    cog = _get_screener_cog()
    return _json_sanitize(await cog.advise_portfolio(
        candidates=req.candidates,
        days=req.days,
        holdings=req.holdings,
        with_financials=req.with_financials,
        capital=req.capital,
        hard_stop_pct=req.hard_stop_pct,
    ))


class ScreenerBusinessModelRequest(BaseModel):
    code: str
    name: Optional[str] = ""
    force: bool = False  # True で保存済みを無視して最新を再分析


@router.post("/business_model", dependencies=[Depends(verify_api_key)])
async def screener_business_model(req: ScreenerBusinessModelRequest):
    """宝石7「ビジネスモデル」＋中計KPI/マテリアリティの定性分析（単一銘柄・Gemini）。
    IR・決算説明資料・中期経営計画・統合報告書を参照して整理する。
    結果は自動保存され、次回はキャッシュを返す（force=True で再分析）。"""
    cog = _get_screener_cog()
    return _json_sanitize(await cog.analyze_business_model(req.code, req.name or "", force=req.force))


class ScreenerBacktestRequest(BaseModel):
    codes: list[str]
    days: int = 750
    rebalance_days: int = 20
    top_k: int = 5
    lookback: int = 60


@router.post("/backtest", dependencies=[Depends(verify_api_key)])
async def screener_backtest(req: ScreenerBacktestRequest):
    """与えた銘柄群で『定期リバランスでモメンタム上位を保有』する回転戦略 vs 等加重 buy&hold を
    過去データで検証する（ポート単位の本格バックテスト・回転コスト込み・決定論的）。
    『毎日入れ替えが買い持ちに勝つか』をコスト込みで裏取りする。"""
    cog = _get_screener_cog()
    return _json_sanitize(await cog.backtest_rotation(
        req.codes, days=req.days, rebalance_days=req.rebalance_days,
        top_k=req.top_k, lookback=req.lookback))


class ScreenerDeepResearchRequest(BaseModel):
    code: str
    name: Optional[str] = ""
    sector: Optional[str] = ""
    force: bool = False


@router.post("/deep_research", dependencies=[Depends(verify_api_key)])
async def screener_deep_research(req: ScreenerDeepResearchRequest):
    """ディープリサーチ（日次ワークフロー③）。『特に強い』1銘柄を Web検索で網羅的に深掘り
    （事業・決算事実・中計KPI・競合・追い風逆風・カタリスト・リスク・バリュエーション文脈）。
    点数/目標株価/値動き予測は出さない（点数は④のエンジン側で確定）。結果はキャッシュ・force で再取得。"""
    cog = _get_screener_cog()
    return _json_sanitize(await cog.deep_research(
        req.code, req.name or "", req.sector or "", force=req.force))


class ScreenerPerformanceRequest(BaseModel):
    holdings: Optional[List[dict]] = None  # 省略時は保有ポートフォリオを自動取得
    days: int = 500


@router.post("/performance", dependencies=[Depends(verify_api_key)])
async def screener_performance(req: ScreenerPerformanceRequest):
    """保有ポートフォリオが市場平均（日経平均等）をアウトパフォームできているかを測定する。
    各ポジションの取得来リターンを同期間のベンチマークと比較し、超過リターンを返す。"""
    cog = _get_screener_cog()
    return _json_sanitize(await cog.measure_performance(days=req.days, holdings=req.holdings))


# ----- 判断の事後検証ループ（売買が正しかったかを後で答え合わせ・学習） -----

class ScreenerDecisionRecordRequest(BaseModel):
    code: str
    name: Optional[str] = ""
    market: Optional[str] = ""
    trade_action: str = "buy"  # buy / sell
    price: Optional[float] = None
    style: Optional[str] = ""


@router.get("/reviews", dependencies=[Depends(verify_api_key)])
async def screener_reviews_list(status: Optional[str] = None, limit: int = 200):
    """記録済みの判断スナップショット（open/partial/verified）を一覧する。"""
    cog = _get_screener_cog()
    return _json_sanitize(await cog.list_decision_reviews(status=status, limit=limit))


@router.post("/reviews/record", dependencies=[Depends(verify_api_key)])
async def screener_reviews_record(req: ScreenerDecisionRecordRequest):
    """売買時の判断スナップショットを手動記録する（通常は売買時に自動記録される）。
    過去の売買を後から登録（バックフィル）したいときに使う。"""
    cog = _get_screener_cog()
    return _json_sanitize(await cog.record_trade_decision(
        code=req.code, name=req.name or "", market=req.market or "",
        trade_action=req.trade_action, price=req.price, style=req.style or "",
    ))


@router.post("/reviews/verify", dependencies=[Depends(verify_api_key)])
async def screener_reviews_verify(force: bool = False):
    """検証期日（20/60営業日）を過ぎた判断を答え合わせし、結果を保存する。"""
    cog = _get_screener_cog()
    return _json_sanitize(await cog.verify_due_decisions(force=force))


@router.get("/reviews/report", dependencies=[Depends(verify_api_key)])
async def screener_reviews_report(horizon: str = "d60"):
    """検証済みの判断を集計し、トレンド/推奨/スタイル/シグナル別の的中率を返す
    （集計前に期日到来分を自動で答え合わせする）。horizon=d20(約1ヶ月)/d60(約3ヶ月)。"""
    cog = _get_screener_cog()
    return _json_sanitize(await cog.decision_review_report(horizon=horizon))


@router.delete("/reviews/{review_id}", dependencies=[Depends(verify_api_key)])
async def screener_reviews_delete(review_id: int):
    cog = _get_screener_cog()
    return _json_sanitize(await cog.delete_decision_review(review_id))


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
