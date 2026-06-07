"""日本株スクリーニング・サービス層。

ユニバースに対して並列でデータを取得し、戦略でスコアリングして上位 N を返す。
Gemini 質的分析（Phase B/C）も提供する。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
from typing import Optional

from config import JST
from services.jp_stock_data_service import StockDataProvider, get_provider
from services.screener_engine import (
    ScreeningResult,
    get_strategy,
    list_strategies,
)


async def _research_cache_get(kind: str, code: str, ttl_days: Optional[int] = None) -> Optional[dict]:
    """銘柄調査結果のキャッシュを取得（app_setting を KV ストアとして利用）。
    ttl_days を超えたものは無効。kind 例: "fin"(財務) / "bizmodel"(定性)。"""
    try:
        from api.database import get_app_setting
        raw = await get_app_setting(f"research.{kind}.{code}", "")
    except Exception:
        return None
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if ttl_days and obj.get("fetched_at"):
        try:
            age = (datetime.datetime.now(JST) - datetime.datetime.fromisoformat(obj["fetched_at"])).days
            if age > ttl_days:
                return None
        except (ValueError, TypeError):
            pass
    return obj


async def _research_cache_set(kind: str, code: str, payload: dict) -> None:
    """銘柄調査結果を自動保存（fetched_at を付与）。"""
    try:
        from api.database import set_app_setting
        data = dict(payload)
        data["fetched_at"] = datetime.datetime.now(JST).isoformat()
        await set_app_setting(f"research.{kind}.{code}", json.dumps(data, ensure_ascii=False))
    except Exception as e:
        logging.debug(f"research cache set 失敗 {kind}.{code}: {e}")


class ScreenerService:
    def __init__(self, provider: Optional[StockDataProvider] = None):
        self.provider = provider or get_provider()

    async def list_styles(self) -> list[dict]:
        return list_strategies()

    async def list_universes(self) -> list[str]:
        return await self.provider.list_universes()

    async def run_screening(
        self,
        style: str,
        top_n: int = 10,
        universe_name: str = "topix500",
        min_market_cap_jpy: Optional[int] = None,
        exclude_sectors: Optional[list[str]] = None,
        enabled_filters: Optional[list[str]] = None,
    ) -> dict:
        """機械スクリーニング (Phase A) を実行する。

        Returns:
            {
                "ok": bool,
                "style": str,
                "data_as_of": str,
                "candidates": [ScreeningResult.to_dict(), ...],
                "scanned": int,
                "qualified": int,
            }
        """
        strategy = get_strategy(style)
        if not strategy:
            return {"ok": False, "error": f"未知のスタイル: {style}"}

        enabled_set = set(enabled_filters) if enabled_filters is not None else None

        universe = await self.provider.get_universe(universe_name)
        if not universe:
            if universe_name == "all":
                return {
                    "ok": False,
                    "error": (
                        "全銘柄リスト (data/jp_universe_all.csv) が未配置です。"
                        "tools/fetch_jpx_universe.py を実行して JPX から取得してください。"
                    ),
                }
            return {"ok": False, "error": f"ユニバースが空: {universe_name}"}

        excluded = set((s or "").strip() for s in (exclude_sectors or []) if s)
        if excluded:
            universe = [u for u in universe if (u.get("sector") or "") not in excluded]

        needs_fundamentals = bool(getattr(strategy, "needs_fundamentals", False))
        results: list[ScreeningResult] = []

        # ファンダ要らないスタイルは並列度を上げる
        max_concurrent = 8 if not needs_fundamentals else 4
        sem = asyncio.Semaphore(max_concurrent)

        # near-miss 候補も収集して、0件時のフォールバックに使う
        near_miss_results: list[ScreeningResult] = []

        async def _process(item: dict):
            code = item["code"]
            name = item.get("name", "")
            sector = item.get("sector", "")
            async with sem:
                try:
                    df = await self.provider.get_ohlcv(code, days=300)
                except Exception as e:
                    logging.debug(f"OHLCV取得エラー {code}: {e}")
                    return None, None
                if df is None:
                    return None, None
                fundamentals = None
                if needs_fundamentals:
                    try:
                        fundamentals = await self.provider.get_fundamentals(code)
                    except Exception as e:
                        logging.debug(f"ファンダ取得エラー {code}: {e}")
                        fundamentals = None
                    if not fundamentals:
                        return None, None
                if min_market_cap_jpy:
                    mcap = (fundamentals or {}).get("market_cap_jpy")
                    if not mcap or mcap < min_market_cap_jpy:
                        if needs_fundamentals:
                            return None, None
                try:
                    hit = strategy.evaluate(code, name, sector, df, fundamentals, enabled_filters=enabled_set)
                    if hit is not None:
                        return hit, None
                    nm = strategy.evaluate(code, name, sector, df, fundamentals, enabled_filters=enabled_set, near_miss=True)
                    return None, nm
                except Exception as e:
                    logging.debug(f"evaluate エラー {code}: {e}")
                    return None, None

        tasks = [_process(it) for it in universe]
        scanned = 0
        for coro in asyncio.as_completed(tasks):
            hit, nm = await coro
            scanned += 1
            if hit is not None:
                results.append(hit)
            elif nm is not None and nm.is_near_miss:
                near_miss_results.append(nm)

        results.sort(key=lambda r: r.score, reverse=True)
        top = results[:top_n]

        # 完全合致が指定数 (top_n) に満たない場合、near-miss（部分合致）の上位で
        # 不足分を埋めて、できる限り常に top_n 件返す。
        # 完全合致を上に・部分合致を下に並べる（部分合致は is_near_miss / failed_filters で区別可能）。
        used_near_miss = False
        if len(top) < top_n and near_miss_results:
            near_miss_results.sort(key=lambda r: r.score, reverse=True)
            shortfall = top_n - len(top)
            fillers = near_miss_results[:shortfall]
            if fillers:
                top = top + fillers
                used_near_miss = True

        # 適用条件の詳細（UI表示用）
        applied_filters = []
        for f in strategy.list_filters():
            on = (enabled_set is None and f["default"]) or (enabled_set is not None and f["key"] in enabled_set)
            if on:
                applied_filters.append({"key": f["key"], "label": f["label"]})

        data_as_of = top[0].data_as_of if top else datetime.datetime.now(JST).strftime("%Y-%m-%d")
        executed_at = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

        return {
            "ok": True,
            "style": style,
            "style_display": strategy.display_name,
            "universe": universe_name,
            "data_as_of": data_as_of,
            "executed_at": executed_at,
            "scanned": scanned,
            "qualified": len(results),
            "applied_filters": applied_filters,
            "used_near_miss": used_near_miss,
            "candidates": [r.to_dict() for r in top],
        }

    async def get_ohlcv_series(self, code: str, days: int = 120) -> dict:
        """1 銘柄の OHLCV を JSON 化して返す（アプリ内チャート表示用）。
        スクリーナーと同じ分割調整済みデータなのでシグナルと一致する。"""
        import math
        days = max(20, min(int(days or 120), 400))
        try:
            df = await self.provider.get_ohlcv(code, days=max(days, 60))
        except Exception as e:
            return {"ok": False, "error": f"取得失敗: {e}"}
        if df is None or len(df) == 0:
            return {"ok": False, "error": "データがありません"}

        def _f(v):
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f if math.isfinite(f) else None

        candles = []
        for idx, row in df.tail(days).iterrows():
            try:
                d = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
            except Exception:
                d = str(idx)[:10]
            vol = row["Volume"] if "Volume" in row else None
            candles.append({
                "date": d,
                "open": _f(row.get("Open")),
                "high": _f(row.get("High")),
                "low": _f(row.get("Low")),
                "close": _f(row.get("Close")),
                "volume": int(vol) if vol == vol and vol is not None else 0,
            })
        return {"ok": True, "code": code, "candles": candles}

    async def analyze_projection(self, code: str, days: int = 750) -> dict:
        """1 銘柄の過去の高値ブレイク後の値動きから、上昇余地・利確目標・損切り目安を返す。
        スクリーニングと同じ分割調整済み OHLCV を使うので、シグナルと整合する。"""
        from services.screener_engine import analyze_breakout_projection
        days = max(250, min(int(days or 750), 1500))
        try:
            df = await self.provider.get_ohlcv(code, days=days)
        except Exception as e:
            return {"ok": False, "error": f"取得失敗: {e}"}
        if df is None or len(df) < 250:
            return {"ok": False, "error": "分析に十分な履歴がありません（約1年以上必要）"}
        try:
            res = analyze_breakout_projection(df)
        except Exception as e:
            return {"ok": False, "error": f"分析に失敗しました: {e}"}
        res["code"] = code
        return res

    # =========================================================
    # ポートフォリオ・アドバイザー：保有銘柄＋候補を横断診断する
    # =========================================================

    async def advise_portfolio(
        self,
        holdings: list[dict],
        candidates: Optional[list[dict]] = None,
        days: int = 300,
        with_financials: bool = False,
    ) -> dict:
        """保有銘柄（holdings）と新規候補（candidates）を、テクニカル×ファンダの
        二重視点で一括診断し、継続保有/縮小/売却・新規買い/見送り・入替候補を返す。

        判定の根拠は決定論的（analyze_position）。holdings の各要素は
        {code, name?, sector?, shares?, avg_cost?} を想定。
        with_financials=True なら EDINET の有報CSVから安全性/キャッシュ指標も取得して
        診断に織り込む（走査が重いので保有＋候補の小集合のみ）。
        """
        from services.screener_engine import (
            analyze_position, compute_relative_metrics, analyze_breakout_projection,
        )

        holdings = holdings or []
        candidates = candidates or []
        held_codes = {str(h.get("code")) for h in holdings if h.get("code")}
        days = max(120, min(int(days or 300), 1000))
        sem = asyncio.Semaphore(4)

        # 財務サマリー（日本株=EDINET、米国株=SEC EDGAR）。
        # 1) まずキャッシュから読む（高速・常時）。一度精査すれば次回から自動で反映される。
        # 2) with_financials=True のときだけ、未キャッシュ分をネットワーク取得して保存。
        all_codes = [str(h.get("code")) for h in holdings if h.get("code")]
        all_codes += [str(c.get("code")) for c in candidates
                      if c.get("code") and str(c.get("code")) not in held_codes]
        financials_by_code: dict[str, dict] = {}
        for c in all_codes:
            cached = await _research_cache_get("fin", c, ttl_days=14)
            if cached and cached.get("summary"):
                financials_by_code[c] = cached["summary"]

        if with_financials:
            missing = [c for c in all_codes if c not in financials_by_code]
            jp_codes = [c for c in missing if c.isdigit()]
            us_codes = [c for c in missing if not c.isdigit()]
            if jp_codes:
                try:
                    from services.edinet_financials import get_financials_for_codes as _edinet
                    for code, s in (await _edinet(jp_codes)).items():
                        financials_by_code[code] = s
                        await _research_cache_set("fin", code, {"summary": s})
                except Exception as e:
                    logging.debug(f"advise EDINET財務取得エラー: {e}")
            if us_codes:
                try:
                    from services.edgar_financials import get_financials_for_codes as _edgar
                    fin_us = await _edgar(us_codes)
                    for c in us_codes:
                        s = fin_us.get(c.upper())
                        if s:
                            financials_by_code[c] = s
                            await _research_cache_set("fin", c, {"summary": s})
                except Exception as e:
                    logging.debug(f"advise EDGAR財務取得エラー: {e}")

        async def _eval(item: dict, held: bool):
            code = str(item.get("code") or "").strip()
            if not code:
                return None
            name = item.get("name") or code
            sector = item.get("sector") or ""
            async with sem:
                try:
                    df = await self.provider.get_ohlcv(code, days=days)
                except Exception as e:
                    logging.debug(f"advise OHLCV取得エラー {code}: {e}")
                    df = None
                if df is None:
                    return {"ok": False, "code": code, "name": name, "sector": sector,
                            "held": held, "error": "価格データ取得失敗"}
                try:
                    fundamentals = await self.provider.get_fundamentals(code)
                except Exception as e:
                    logging.debug(f"advise ファンダ取得エラー {code}: {e}")
                    fundamentals = None
                try:
                    res = analyze_position(
                        df, fundamentals,
                        avg_cost=item.get("avg_cost") if held else None,
                        held=held,
                        financials=financials_by_code.get(code),
                    )
                except Exception as e:
                    logging.debug(f"advise analyze_position エラー {code}: {e}")
                    res = {"ok": False, "error": f"診断失敗: {e}"}
                # 新規候補は「利確目安(projection)」と整合させる。過熱/リスクリワード劣後なら
                # 新規買いは見送り（BUY→WATCH）。保有銘柄は利を伸ばすため過熱でも維持。
                if (not held) and res.get("ok"):
                    try:
                        proj = analyze_breakout_projection(df)
                    except Exception:
                        proj = None
                    if proj and proj.get("ok"):
                        verdict_txt = proj.get("verdict") or ""
                        overheated = verdict_txt.startswith("過熱")
                        avoid = verdict_txt.startswith("見送り")
                        res["projection"] = {
                            "verdict": verdict_txt,
                            "risk_reward": proj.get("risk_reward"),
                            "remaining_estimate_pct": proj.get("remaining_estimate_pct"),
                            "overheated": overheated,
                        }
                        # 利確目安が「見送り寄り」(リスクリワード劣後)なら新規買いは見送り(BUY→WATCH)。
                        # 「過熱気味」は BUY のまま残すが overheated フラグを立て、入替候補からは除外する
                        # （後段の rotations で利用）。これで利確目安と一括診断の判断が食い違わない。
                        if res["verdict"]["action"] == "BUY" and avoid:
                            res["verdict"]["action"] = "WATCH"
                            res["verdict"]["action_label"] = "ウォッチ（妙味薄）"
                            res["verdict"]["note"] = (
                                "テクニカル・ファンダは買い方向だが、利確目安が「見送り寄り」"
                                "（直近高値まで近く損切り幅に対し利幅が小さい）。新規買いは見送り。"
                            )
                        elif res["verdict"]["action"] == "BUY" and overheated:
                            res["verdict"]["note"] += (
                                "（ただし利確目安は『過熱気味』。新規・入替で飛び乗るより押し目待ちが無難）"
                            )
            res["code"] = code
            res["name"] = name
            res["sector"] = sector
            res["held"] = held
            # 入れ替えは同一市場内で行うため、市場を判定して付与（4桁数字=日本株）
            res["market"] = item.get("market") or ("JP" if code.isdigit() else "US")
            if held:
                res["shares"] = item.get("shares")
            return res

        hold_results = await asyncio.gather(*[_eval(h, True) for h in holdings])
        cand_items = [c for c in candidates if str(c.get("code")) not in held_codes]
        cand_results = await asyncio.gather(*[_eval(c, False) for c in cand_items])

        holdings_out = [r for r in hold_results if r]
        candidates_out = [r for r in cand_results if r]

        ok_all = [r for r in holdings_out + candidates_out if r.get("ok")]
        # 宝石5：他社比較で相対スコア（blended_score）を付与（in place）
        compute_relative_metrics(ok_all)

        # 事後検証の学習結果を活用：このトレンド状態の判断が過去どれだけ的中したかを併記。
        # （ここでは answering 中に重いネットワーク検証はせず、保存済みの集計のみ参照）
        track = None
        try:
            track = await self.decision_review_report(horizon="d60", auto_verify=False)
            tr_by_trend = {b["key"]: b for b in (track.get("by_trend") or []) if b.get("key")}
            for r in ok_all:
                st = (r.get("trend") or {}).get("state_label")
                b = tr_by_trend.get(st)
                if b and b.get("hit_rate") is not None:
                    r["track_record"] = {
                        "trend_state": st,
                        "hit_rate": b["hit_rate"],
                        "samples": b["win"] + b["lose"],
                        "avg_excess_pct": b.get("avg_excess_pct"),
                    }
        except Exception as e:
            logging.debug(f"advise track_record 付与エラー: {e}")

        def _rk(r):
            v = r.get("blended_score")
            return v if v is not None else (r.get("score") or 0)

        ranking = sorted(ok_all, key=_rk, reverse=True)
        ranking_brief = [{
            "code": r["code"], "name": r["name"], "held": r["held"],
            "action": r["verdict"]["action"], "action_label": r["verdict"]["action_label"],
            "score": r.get("score"), "blended_score": r.get("blended_score"),
        } for r in ranking]

        sells = [r for r in holdings_out
                 if r.get("ok") and r["verdict"]["action"] in ("SELL", "TRIM")]
        # 入替先(buys)からは利確目安で「過熱気味」の銘柄を除外（高値掴みの入替を避ける）。
        buys = [r for r in candidates_out
                if r.get("ok") and r["verdict"]["action"] == "BUY"
                and not (r.get("projection") or {}).get("overheated")]
        # 入れ替えは同一市場内のみ（日本株↔日本株、米国株↔米国株）
        rotations = []
        for mkt in ("JP", "US"):
            sells_weak = sorted([s for s in sells if s.get("market") == mkt], key=_rk)
            buys_strong = sorted([b for b in buys if b.get("market") == mkt], key=_rk, reverse=True)
            mkt_label = "日本株" if mkt == "JP" else "米国株"
            for s, b in zip(sells_weak, buys_strong):
                if _rk(b) - _rk(s) >= 10:
                    rotations.append({
                        "sell": {"code": s["code"], "name": s["name"], "score": _rk(s),
                                 "action_label": s["verdict"]["action_label"]},
                        "buy": {"code": b["code"], "name": b["name"], "score": _rk(b)},
                        "market": mkt,
                        "reason": (f"[{mkt_label}] {s['name']}は{s['verdict']['action_label']}水準"
                                   f"（総合{_rk(s)}点）。より強い{b['name']}（総合{_rk(b)}点）へ入替を検討。"),
                    })

        keep = [r for r in holdings_out if r.get("ok") and r["verdict"]["action"] in ("HOLD", "HOLD_WATCH")]
        buy_count = sum(1 for r in candidates_out if r.get("ok") and r["verdict"]["action"] == "BUY")
        as_of = next((r.get("as_of") for r in ok_all if r.get("as_of")), "")
        summary = (f"保有{len(holdings_out)}銘柄: 継続{len(keep)}・縮小/売却{len(sells)}。"
                   f"新規候補{len(candidates_out)}銘柄中、両方で買い{buy_count}件"
                   f"（うち入替向き{len(buys)}件）。入替提案{len(rotations)}件。")

        # 「市場に翻弄されて下手に売らない」方針の反映：
        # 過去の売り判断が市場対比で裏目（握っていた方が得だった）傾向で、かつ今回も
        # 売却/縮小を提案しているなら、固有の悪材料が無いか再確認を促す注意を添える。
        over_trading_caution = None
        tv = (track or {}).get("trading_value_add") or {}
        if sells and tv.get("over_trading"):
            over_trading_caution = (
                f"⚠️ 今回 売却/縮小を{len(sells)}件提案していますが、過去の売り判断は売却後も"
                f"平均で市場を{tv.get('avg_excess_after_exit_pct'):+.1f}%上回っています"
                "（＝握っていた方が得だった傾向）。相場全体の地合いによる下げを、固有の悪材料と"
                "取り違えていないか確認してください。トレンド崩れ＋ファンダ悪化が揃っていない"
                "売りは見送る方が無難です。"
            )

        return {
            "ok": True,
            "as_of": as_of,
            "summary": summary,
            "with_financials": with_financials,
            "financials_count": len(financials_by_code),
            "holdings": holdings_out,
            "candidates": candidates_out,
            "ranking": ranking_brief,
            "rotations": rotations,
            "over_trading_caution": over_trading_caution,
            # 過去の判断の事後検証から得た「効いているトレンド状態」の学習結果（参考）
            "decision_track_record": {
                "summary": (track or {}).get("summary"),
                "overall_hit_rate": (track or {}).get("overall_hit_rate"),
                "verified_count": (track or {}).get("verified_count"),
                "by_trend": (track or {}).get("by_trend"),
                "trading_value_add": tv or None,
                "market_beta_note": (track or {}).get("market_beta_note"),
            } if track else None,
        }

    # =========================================================
    # パフォーマンス測定：保有ポートフォリオ vs 市場平均（ベンチマーク）
    # =========================================================

    # 市場ごとの代表ベンチマーク（yfinance シンボル → 表示名）
    _BENCHMARKS = {"JP": "^N225", "US": "^GSPC"}
    _BENCH_LABELS = {"^N225": "日経平均", "^GSPC": "S&P500", "1306.T": "TOPIX(1306)"}

    async def measure_performance(self, holdings: list[dict], days: int = 500) -> dict:
        """保有銘柄が市場平均（ベンチマーク）をアウトパフォームできているかを測定する。

        各ポジションの取得来リターン((現値-平均取得単価)/平均取得単価)を、同期間
        （取得日→現在）のベンチマーク・リターンと比較し、超過リターン(excess)を出す。
        ポートフォリオ全体ではコスト基準で加重平均し、対ベンチマーク超過を返す。
        """
        import datetime as _dt
        holdings = holdings or []
        today = _dt.datetime.now(JST).date()

        def _parse_date(s):
            try:
                return _dt.date.fromisoformat(str(s)[:10])
            except (ValueError, TypeError):
                return None

        entries = [_parse_date(h.get("opened_at")) for h in holdings]
        valid_entries = [d for d in entries if d]
        if valid_entries:
            span_days = (today - min(valid_entries)).days + 30
        else:
            span_days = days
        span_days = max(120, min(int(span_days), 2000))

        # 出現する市場のベンチマークだけ取得
        markets = {(h.get("market") or "JP") for h in holdings} or {"JP"}
        bench_df: dict[str, tuple] = {}
        for mk in markets:
            sym = self._BENCHMARKS.get(mk, "^N225")
            try:
                bdf = await self.provider.get_ohlcv(sym, days=span_days)
            except Exception as e:
                logging.debug(f"ベンチマーク取得エラー {sym}: {e}")
                bdf = None
            bench_df[mk] = (sym, bdf)

        def _annualize(ret_pct, days):
            """単純リターン(%)を年率換算(%)。保有30日未満は不安定なので None。"""
            if ret_pct is None or not days or days < 30:
                return None
            try:
                return round(((1 + ret_pct / 100.0) ** (365.0 / days) - 1) * 100, 1)
            except (ValueError, OverflowError, ZeroDivisionError):
                return None

        def _bench_return(mk, entry_date):
            sym, bdf = bench_df.get(mk, (None, None))
            if bdf is None or bdf.empty or not entry_date:
                return sym, None
            try:
                import pandas as pd  # type: ignore
                b_now = float(bdf["Close"].iloc[-1])
                b_entry = bdf["Close"].asof(pd.Timestamp(entry_date))
                b_entry = float(b_entry) if b_entry == b_entry else None  # NaN 除外
                if b_entry and b_entry > 0:
                    return sym, round((b_now - b_entry) / b_entry * 100, 1)
            except Exception:
                pass
            return sym, None

        sem = asyncio.Semaphore(4)

        async def _eval(h: dict):
            code = str(h.get("code") or "").strip()
            if not code:
                return None
            name = h.get("name") or code
            try:
                shares = float(h.get("shares") or 0)
                cost = float(h.get("avg_cost") or 0)
            except (TypeError, ValueError):
                shares, cost = 0.0, 0.0
            mk = h.get("market") or "JP"
            entry = _parse_date(h.get("opened_at"))
            async with sem:
                try:
                    df = await self.provider.get_ohlcv(code, days=span_days)
                except Exception as e:
                    logging.debug(f"performance OHLCV取得エラー {code}: {e}")
                    df = None
            if df is None or df.empty or cost <= 0:
                return {"ok": False, "code": code, "name": name, "error": "価格/取得単価が不足"}
            cur = float(df["Close"].iloc[-1])
            pos_ret = round((cur - cost) / cost * 100, 1)
            sym, bench_ret = _bench_return(mk, entry)
            excess = round(pos_ret - bench_ret, 1) if bench_ret is not None else None
            holding_days = (today - entry).days if entry else None
            ret_ann = _annualize(pos_ret, holding_days)
            bench_ann = _annualize(bench_ret, holding_days)
            excess_ann = round(ret_ann - bench_ann, 1) if (ret_ann is not None and bench_ann is not None) else None
            return {
                "ok": True, "code": code, "name": name, "market": mk,
                "shares": shares, "avg_cost": round(cost, 2), "current_price": round(cur, 2),
                "cost_value": round(cost * shares, 0), "market_value": round(cur * shares, 0),
                "return_pct": pos_ret,
                "benchmark": self._BENCH_LABELS.get(sym, sym),
                "benchmark_return_pct": bench_ret,
                "excess_pct": excess,
                "outperforming": (excess is not None and excess > 0),
                "holding_days": holding_days,
                "return_annual_pct": ret_ann,
                "benchmark_annual_pct": bench_ann,
                "excess_annual_pct": excess_ann,
                "opened_at": (entry.isoformat() if entry else None),
            }

        results = [r for r in await asyncio.gather(*[_eval(h) for h in holdings]) if r]
        ok_rows = [r for r in results if r.get("ok")]

        total_cost = sum(r["cost_value"] for r in ok_rows)
        total_value = sum(r["market_value"] for r in ok_rows)
        port_ret = round((total_value - total_cost) / total_cost * 100, 1) if total_cost > 0 else None

        # コスト加重のベンチマーク・リターン（bench 既知のポジションだけで再正規化）
        w_rows = [r for r in ok_rows if r.get("benchmark_return_pct") is not None and r["cost_value"] > 0]
        w_total = sum(r["cost_value"] for r in w_rows)
        port_bench = (round(sum(r["benchmark_return_pct"] * r["cost_value"] for r in w_rows) / w_total, 1)
                      if w_total > 0 else None)
        port_excess = (round(port_ret - port_bench, 1)
                       if (port_ret is not None and port_bench is not None) else None)

        # 保有期間を考慮した年率換算（コスト加重の平均保有日数で年率化）
        hd_rows = [r for r in ok_rows if r.get("holding_days") and r["cost_value"] > 0]
        hd_total = sum(r["cost_value"] for r in hd_rows)
        avg_holding_days = (round(sum(r["holding_days"] * r["cost_value"] for r in hd_rows) / hd_total)
                            if hd_total > 0 else None)
        port_ret_ann = _annualize(port_ret, avg_holding_days)
        port_bench_ann = _annualize(port_bench, avg_holding_days)
        port_excess_ann = (round(port_ret_ann - port_bench_ann, 1)
                           if (port_ret_ann is not None and port_bench_ann is not None) else None)

        bench_names = sorted({r["benchmark"] for r in ok_rows if r.get("benchmark")})
        as_of = ""
        for _mk, (_sym, bdf) in bench_df.items():
            if bdf is not None and not bdf.empty:
                try:
                    as_of = bdf.index[-1].strftime("%Y-%m-%d")
                except Exception:
                    pass
                break

        ann_note = (f"／ 年率換算では {port_ret_ann:+.1f}% vs {port_bench_ann:+.1f}%"
                    f"（超過 {port_excess_ann:+.1f}%・平均保有{avg_holding_days}日）"
                    if port_excess_ann is not None else "")
        if port_excess is None:
            summary = "ベンチマーク比較に必要なデータ（取得日など）が不足しています。"
        elif port_excess > 0:
            summary = (f"✅ 市場平均をアウトパフォーム中：ポートフォリオ {port_ret:+.1f}% vs "
                       f"{'/'.join(bench_names)} {port_bench:+.1f}%（超過 {port_excess:+.1f}%）{ann_note}")
        else:
            summary = (f"⚠️ 市場平均にアンダーパフォーム：ポートフォリオ {port_ret:+.1f}% vs "
                       f"{'/'.join(bench_names)} {port_bench:+.1f}%（超過 {port_excess:+.1f}%）{ann_note}")

        # 寄与の大きい順（超過×コスト）に並べる
        ok_rows.sort(key=lambda r: (r.get("excess_pct") if r.get("excess_pct") is not None else -999), reverse=True)

        return {
            "ok": True,
            "as_of": as_of,
            "benchmarks": bench_names,
            "summary": summary,
            "portfolio": {
                "return_pct": port_ret,
                "benchmark_return_pct": port_bench,
                "excess_pct": port_excess,
                "outperforming": (port_excess is not None and port_excess > 0),
                "avg_holding_days": avg_holding_days,
                "return_annual_pct": port_ret_ann,
                "benchmark_annual_pct": port_bench_ann,
                "excess_annual_pct": port_excess_ann,
                "outperforming_annual": (port_excess_ann is not None and port_excess_ann > 0),
                "total_cost": round(total_cost, 0),
                "total_value": round(total_value, 0),
            },
            "positions": ok_rows + [r for r in results if not r.get("ok")],
        }

    # =========================================================
    # 判断の事後検証ループ：売買時に診断を記録し、後で答え合わせして学習する
    # =========================================================

    # 検証の評価期間（営業日）。20≈1ヶ月、60≈3ヶ月の2段階で採点する。
    _REVIEW_HORIZONS = [("d20", 20), ("d60", 60)]
    # 「正解/不正解」を分ける超過リターンのデッドバンド（%）。±この幅は「引分」。
    _REVIEW_DEADBAND_PCT = 1.0

    async def record_trade_decision(
        self,
        code: str,
        name: str = "",
        market: str = "",
        trade_action: str = "buy",
        price: Optional[float] = None,
        style: str = "",
    ) -> dict:
        """売買が成立した瞬間に、その銘柄の診断スナップショット（テクニカル状態・推奨
        アクション・利確目安・約定価格）を decision_reviews に保存する。

        後で verify_due_decisions が答え合わせの基準に使う。価格やファンダが取れなくても
        最低限の記録は残し、検証時に現値を取りに行く。"""
        from api.database import decision_review_save
        from services.screener_engine import analyze_position, analyze_breakout_projection

        code = str(code or "").strip()
        if not code:
            return {"ok": False, "error": "code が空です"}
        market = market or ("JP" if code.isdigit() else "US")
        is_exit = str(trade_action).lower() == "sell"

        try:
            df = await self.provider.get_ohlcv(code, days=400)
        except Exception as e:
            logging.debug(f"record_trade_decision OHLCV取得エラー {code}: {e}")
            df = None

        rec_action = ""
        trend_state = ""
        score = None
        signals: list[dict] = []
        projection_snapshot: dict = {}
        last_close = None

        if df is not None and len(df) >= 60:
            try:
                fundamentals = await self.provider.get_fundamentals(code)
            except Exception:
                fundamentals = None
            try:
                res = analyze_position(
                    df, fundamentals,
                    avg_cost=(price if is_exit else None),
                    held=is_exit,
                )
            except Exception as e:
                logging.debug(f"record_trade_decision analyze_position エラー {code}: {e}")
                res = {"ok": False}
            if res.get("ok"):
                verdict = res.get("verdict") or {}
                trend = res.get("trend") or {}
                rec_action = verdict.get("action") or ""
                trend_state = trend.get("state_label") or ""
                score = res.get("score")
                last_close = res.get("last_close")
                signals = [
                    {"key": "trend_state", "label": "トレンド", "value": trend.get("state_label")},
                    {"key": "perfect_order", "label": "パーフェクトオーダー", "value": bool(trend.get("perfect_order"))},
                    {"key": "above_sma25", "label": "25日線上", "value": bool(trend.get("above_fast"))},
                    {"key": "above_sma75", "label": "75日線上", "value": bool(trend.get("above_mid"))},
                    {"key": "below_trailing_stop", "label": "トレイリングストップ割れ", "value": bool(trend.get("below_trailing_stop"))},
                ]
            try:
                proj = analyze_breakout_projection(df)
            except Exception:
                proj = None
            if proj and proj.get("ok"):
                projection_snapshot = {
                    "verdict": proj.get("verdict"),
                    "risk_reward": proj.get("risk_reward"),
                    "remaining_estimate_pct": proj.get("remaining_estimate_pct"),
                }

        rid = await decision_review_save({
            "decided_at": datetime.datetime.now(JST).isoformat(),
            "code": code,
            "name": name or code,
            "market": market,
            "trade_action": str(trade_action).lower(),
            "rec_action": rec_action,
            "trend_state": trend_state,
            "price_at_decision": price if price is not None else last_close,
            "score": score,
            "signals": signals,
            "projection": projection_snapshot,
            "style": style,
        })
        return {"ok": True, "id": rid, "snapshot": bool(signals)}

    async def verify_due_decisions(self, force: bool = False) -> dict:
        """検証期日（20/60営業日）を過ぎた判断を答え合わせし、結果を保存する。

        各判断について「その後のリターン − 同期間のベンチマーク超過」を算出する。
        買い/保有は超過プラスで「正解」、売り/縮小は超過マイナス（＝売って正解）で
        「正解」とする（符号を反転）。force=True で既存チェックポイントも再計算。"""
        import datetime as _dt
        import pandas as pd  # type: ignore
        from api.database import (
            decision_review_list_pending, decision_review_update_checkpoints,
        )

        pending = await decision_review_list_pending()
        if not pending:
            return {"ok": True, "checked": 0, "updated": 0, "results": []}

        # ベンチマークは市場ごとにまとめて1回だけ取得
        markets = {p.get("market") or "JP" for p in pending}
        bench_df: dict[str, tuple] = {}
        for mk in markets:
            sym = self._BENCHMARKS.get(mk, "^N225")
            try:
                bench_df[mk] = (sym, await self.provider.get_ohlcv(sym, days=500))
            except Exception as e:
                logging.debug(f"verify ベンチマーク取得エラー {sym}: {e}")
                bench_df[mk] = (sym, None)

        def _parse_date(s):
            try:
                return _dt.date.fromisoformat(str(s)[:10])
            except (ValueError, TypeError):
                return None

        def _series_after(d, date):
            """date 以降の取引日に限定した Close 系列を返す（なければ None）。"""
            if d is None or getattr(d, "empty", True):
                return None
            try:
                after = d[d.index >= pd.Timestamp(date)]
            except Exception:
                return None
            return after if len(after) > 0 else None

        updated = 0
        results: list[dict] = []
        sem = asyncio.Semaphore(4)

        async def _verify(p: dict):
            nonlocal updated
            code = str(p.get("code") or "")
            decided = _parse_date(p.get("decided_at"))
            if not code or not decided:
                return
            async with sem:
                try:
                    df = await self.provider.get_ohlcv(code, days=500)
                except Exception as e:
                    logging.debug(f"verify OHLCV取得エラー {code}: {e}")
                    df = None
            after = _series_after(df, decided)
            if after is None:
                return
            base_price = p.get("price_at_decision")
            if not base_price or base_price <= 0:
                base_price = float(after["Close"].iloc[0])
            if not base_price or base_price <= 0:
                return

            mk = p.get("market") or "JP"
            _sym, bdf = bench_df.get(mk, (None, None))
            b_after = _series_after(bdf, decided)
            b_base = float(b_after["Close"].iloc[0]) if b_after is not None else None

            ta = (p.get("trade_action") or "").lower()
            ra = (p.get("rec_action") or "").upper()
            is_exit = ta == "sell" or ra in ("SELL", "TRIM")

            checkpoints = dict(p.get("checkpoints") or {})
            changed = False
            for key, n in self._REVIEW_HORIZONS:
                if key in checkpoints and not force:
                    continue
                if len(after) <= n:
                    continue  # まだ期日に達していない
                fwd_price = float(after["Close"].iloc[n])
                ret = (fwd_price - base_price) / base_price * 100
                bench_ret = None
                if b_base and b_after is not None and len(b_after) > n:
                    bench_ret = (float(b_after["Close"].iloc[n]) - b_base) / b_base * 100
                excess = (ret - bench_ret) if bench_ret is not None else None
                # 売却/縮小は「売って正解＝その後下げた/劣後した」を正解にするため符号反転
                base_metric = excess if excess is not None else ret
                signed = -base_metric if is_exit else base_metric
                if signed > self._REVIEW_DEADBAND_PCT:
                    outcome = "正解"
                elif signed < -self._REVIEW_DEADBAND_PCT:
                    outcome = "不正解"
                else:
                    outcome = "引分"
                checkpoints[key] = {
                    "return_pct": round(ret, 1),
                    "benchmark_return_pct": round(bench_ret, 1) if bench_ret is not None else None,
                    "excess_pct": round(excess, 1) if excess is not None else None,
                    "outcome": outcome,
                    "verified_at": _dt.datetime.now(JST).isoformat(),
                }
                changed = True

            if changed:
                status = ("verified"
                          if all(k in checkpoints for k, _ in self._REVIEW_HORIZONS)
                          else "partial")
                await decision_review_update_checkpoints(p["id"], checkpoints, status)
                updated += 1
                results.append({
                    "id": p["id"], "code": code, "name": p.get("name"),
                    "trade_action": ta, "status": status, "checkpoints": checkpoints,
                })

        await asyncio.gather(*[_verify(p) for p in pending])
        return {"ok": True, "checked": len(pending), "updated": updated, "results": results}

    async def decision_review_report(self, horizon: str = "d60", auto_verify: bool = True) -> dict:
        """検証済みの判断を集計し、トレンド状態・推奨アクション・スタイル・シグナル別の
        的中率を返す。次回スクリーニング/一括診断で候補の信頼度として併記するための学習結果。

        auto_verify=True なら集計前に期日到来分を答え合わせしてから集計する。"""
        from api.database import decision_review_list

        if auto_verify:
            try:
                await self.verify_due_decisions()
            except Exception as e:
                logging.debug(f"decision_review_report 事前verifyエラー: {e}")

        if horizon not in ("d20", "d60"):
            horizon = "d60"
        rows = await decision_review_list(limit=1000)

        def _cp(r):
            cp = (r.get("checkpoints") or {}).get(horizon)
            return cp if (cp and cp.get("outcome")) else None

        verified = [r for r in rows if _cp(r)]

        def _aggregate(key_fn):
            buckets: dict[str, dict] = {}
            for r in verified:
                k = key_fn(r)
                if not k:
                    continue
                cp = _cp(r)
                b = buckets.setdefault(k, {"total": 0, "win": 0, "lose": 0, "draw": 0,
                                           "sum_excess": 0.0, "n_excess": 0})
                b["total"] += 1
                o = cp.get("outcome")
                if o == "正解":
                    b["win"] += 1
                elif o == "不正解":
                    b["lose"] += 1
                else:
                    b["draw"] += 1
                ex = cp.get("excess_pct")
                if ex is not None:
                    b["sum_excess"] += ex
                    b["n_excess"] += 1
            out = []
            for k, b in buckets.items():
                decisive = b["win"] + b["lose"]
                out.append({
                    "key": k, "total": b["total"], "win": b["win"],
                    "lose": b["lose"], "draw": b["draw"],
                    "hit_rate": round(b["win"] / decisive * 100, 1) if decisive else None,
                    "avg_excess_pct": round(b["sum_excess"] / b["n_excess"], 1) if b["n_excess"] else None,
                })
            out.sort(key=lambda x: (x["hit_rate"] if x["hit_rate"] is not None else -1, x["total"]),
                     reverse=True)
            return out

        by_trend = _aggregate(lambda r: r.get("trend_state"))
        by_action = _aggregate(lambda r: r.get("rec_action"))
        by_style = _aggregate(lambda r: r.get("style"))

        # シグナル別（スナップショット時に True だったカテゴリフラグごとの的中率）
        sig_buckets: dict[str, dict] = {}
        for r in verified:
            cp = _cp(r)
            for s in (r.get("signals") or []):
                if s.get("value") is True:
                    k = s.get("label") or s.get("key")
                    b = sig_buckets.setdefault(k, {"total": 0, "win": 0, "lose": 0})
                    b["total"] += 1
                    if cp.get("outcome") == "正解":
                        b["win"] += 1
                    elif cp.get("outcome") == "不正解":
                        b["lose"] += 1
        by_signal = []
        for k, b in sig_buckets.items():
            decisive = b["win"] + b["lose"]
            by_signal.append({
                "key": k, "total": b["total"], "win": b["win"], "lose": b["lose"],
                "hit_rate": round(b["win"] / decisive * 100, 1) if decisive else None,
            })
        by_signal.sort(key=lambda x: (x["hit_rate"] if x["hit_rate"] is not None else -1, x["total"]),
                       reverse=True)

        # --- 相場に翻弄されない判定の裏付け：生リターン vs 市場超過 ＆ 売買の付加価値 ---
        def _signed_outcome(val, is_exit):
            if val is None:
                return None
            s = -val if is_exit else val
            if s > self._REVIEW_DEADBAND_PCT:
                return "正解"
            if s < -self._REVIEW_DEADBAND_PCT:
                return "不正解"
            return "引分"

        raw_win = raw_lose = ex_win = ex_lose = 0
        exit_excess_vals: list[float] = []
        for r in verified:
            cp = _cp(r)
            ta = (r.get("trade_action") or "").lower()
            ra = (r.get("rec_action") or "").upper()
            is_exit = ta == "sell" or ra in ("SELL", "TRIM")
            ro = _signed_outcome(cp.get("return_pct"), is_exit)
            if ro == "正解":
                raw_win += 1
            elif ro == "不正解":
                raw_lose += 1
            if cp.get("excess_pct") is not None:
                eo = _signed_outcome(cp.get("excess_pct"), is_exit)
                if eo == "正解":
                    ex_win += 1
                elif eo == "不正解":
                    ex_lose += 1
                if is_exit:
                    exit_excess_vals.append(cp["excess_pct"])

        raw_hit = round(raw_win / (raw_win + raw_lose) * 100, 1) if (raw_win + raw_lose) else None
        excess_hit = round(ex_win / (ex_win + ex_lose) * 100, 1) if (ex_win + ex_lose) else None
        market_beta_note = None
        if raw_hit is not None and excess_hit is not None:
            market_beta_note = (
                f"生リターン基準の的中率 {raw_hit}% に対し、市場超過で測ると {excess_hit}%。"
                "その差は『相場の地合い』が生んだ見かけの勝ち負けです。本機能は超過リターンで"
                "採点するので、地合いに左右されない実力を見ています。"
            )

        # 売買の付加価値：売り判断のあと、その銘柄が市場をどれだけ上回ったか（平均）。
        # 正＝売った後も市場に勝っていた＝「握っていた方が得だった」傾向。
        avg_exit_excess = (round(sum(exit_excess_vals) / len(exit_excess_vals), 1)
                           if exit_excess_vals else None)
        tv_msg = None
        over_trading = False
        if avg_exit_excess is not None:
            if avg_exit_excess > 1.0:
                over_trading = True
                tv_msg = (f"売却した銘柄は、その後も平均で市場を {avg_exit_excess:+.1f}% 上回っています。"
                          "相場全体の流れに乗っているだけの銘柄を、固有の悪材料が無いのに手放して"
                          "いる可能性があります。『市場に翻弄されて下手に売らない』方針が有効な兆候です。")
            elif avg_exit_excess < -1.0:
                tv_msg = (f"売却した銘柄は、その後 平均で市場を {avg_exit_excess:+.1f}% 下回っています。"
                          "売り判断は下落回避として機能しています（固有の悪化を捉えられている）。")
            else:
                tv_msg = "売却後の市場超過はほぼ中立。売買による付加価値は今のところ小さいです。"
        trading_value_add = {
            "exit_decisions": len(exit_excess_vals),
            "avg_excess_after_exit_pct": avg_exit_excess,
            "over_trading": over_trading,
            "message": tv_msg,
        }
        philosophy_note = (
            "個別銘柄の短期の値動きは市場全体（ベータ）に強く連動します。だから売買の巧拙は"
            "『市場に対する超過リターン』で測るべきで、相場の上下に合わせて頻繁に売買すると、"
            "コストとタイミングのズレで実力以上に成績を落としがちです。固有の悪材料"
            "（トレンド崩れ＋ファンダ悪化）が無い限り、握り続ける方が得策なことが多い——"
            "という考え方は概ね正しく、本機能はこの前提で設計されています。"
        )

        total = len(verified)
        wins = sum(1 for r in verified if _cp(r).get("outcome") == "正解")
        loses = sum(1 for r in verified if _cp(r).get("outcome") == "不正解")
        decisive = wins + loses
        overall_hit = round(wins / decisive * 100, 1) if decisive else None
        pending_count = sum(1 for r in rows if r.get("status") in ("open", "partial"))

        if total:
            summary = (f"検証済み{total}件（{horizon}・{'約1ヶ月後' if horizon == 'd20' else '約3ヶ月後'}）："
                       f"的中{wins}・外れ{loses}・引分{total - decisive}。"
                       f"勝敗ベースの的中率 {overall_hit}%。検証待ち{pending_count}件。")
        else:
            summary = ("まだ検証済みの判断がありません。売買から約1〜3ヶ月後に自動で答え合わせされます。"
                       f"（記録済み{len(rows)}件・検証待ち{pending_count}件）")

        return {
            "ok": True,
            "horizon": horizon,
            "verified_count": total,
            "pending_count": pending_count,
            "recorded_count": len(rows),
            "overall_hit_rate": overall_hit,
            "raw_hit_rate": raw_hit,
            "excess_hit_rate": excess_hit,
            "market_beta_note": market_beta_note,
            "trading_value_add": trading_value_add,
            "philosophy_note": philosophy_note,
            "summary": summary,
            "by_trend": by_trend,
            "by_action": by_action,
            "by_style": by_style,
            "by_signal": by_signal,
        }

    # =========================================================
    # スタイル横断フィルタ：既存候補を別スタイルで再評価する
    # =========================================================

    async def apply_secondary_style(
        self,
        candidates: list[dict],
        secondary_style: str,
        enabled_filters: Optional[list[str]] = None,
    ) -> dict:
        """機械スクリーニング結果（candidates）について、別スタイルの条件で再評価する。

        Returns:
            {ok, secondary_style, secondary_display, items: [
                {code, name, sector, secondary_pass, secondary_score, secondary_signals}
            ]}
        """
        strategy = get_strategy(secondary_style)
        if not strategy:
            return {"ok": False, "error": f"未知のスタイル: {secondary_style}"}

        enabled_set = set(enabled_filters) if enabled_filters is not None else None
        needs_fundamentals = bool(getattr(strategy, "needs_fundamentals", False))

        sem = asyncio.Semaphore(6)

        async def _eval_one(c: dict) -> dict:
            code = c.get("code")
            name = c.get("name", "")
            sector = c.get("sector", "")
            if not code:
                return {"code": code, "name": name, "sector": sector, "secondary_pass": False, "error": "コード欠落"}
            async with sem:
                try:
                    df = await self.provider.get_ohlcv(code, days=300)
                except Exception as e:
                    return {"code": code, "name": name, "sector": sector, "secondary_pass": False, "error": f"OHLCV取得失敗: {e}"}
                if df is None:
                    return {"code": code, "name": name, "sector": sector, "secondary_pass": False, "error": "データ無し"}
                fundamentals = None
                if needs_fundamentals:
                    try:
                        fundamentals = await self.provider.get_fundamentals(code)
                    except Exception:
                        fundamentals = None
                try:
                    hit = strategy.evaluate(code, name, sector, df, fundamentals, enabled_filters=enabled_set)
                    if hit is not None:
                        d = hit.to_dict()
                        return {
                            "code": code, "name": name, "sector": sector,
                            "secondary_pass": True,
                            "secondary_score": d.get("score"),
                            "secondary_signals": d.get("signals", []),
                        }
                    nm = strategy.evaluate(code, name, sector, df, fundamentals, enabled_filters=enabled_set, near_miss=True)
                    sigs = nm.to_dict().get("signals", []) if nm is not None else []
                    return {
                        "code": code, "name": name, "sector": sector,
                        "secondary_pass": False,
                        "secondary_score": (nm.score if nm is not None else 0),
                        "secondary_signals": sigs,
                    }
                except Exception as e:
                    return {"code": code, "name": name, "sector": sector, "secondary_pass": False, "error": str(e)}

        items = await asyncio.gather(*[_eval_one(c) for c in (candidates or [])])
        return {
            "ok": True,
            "secondary_style": secondary_style,
            "secondary_display": strategy.display_name,
            "items": list(items),
        }

    # =========================================================
    # Phase B/C: Gemini 質的分析（呼び出しは ScreenerCog 側で行う）
    # =========================================================

    @staticmethod
    def build_phase_b_prompt(candidate: dict, constitution_excerpt: str = "") -> str:
        """Phase B: 1 銘柄の質的補強プロンプトを生成する。
        Gemini に新規数値を生成させない / 出典 URL を必須にする。"""
        signals_text = "\n".join(
            f"- {s['name']}: {s['value']} (基準 {s['threshold']}, "
            f"{'通過' if s['passed'] else '未通過'}, 出典: {s['source']})"
            for s in candidate.get("signals", [])
        )
        snapshot = candidate.get("price_snapshot") or {}
        snapshot_text = ", ".join(f"{k}={v}" for k, v in snapshot.items())

        return (
            "あなたは予測者ではなく、根拠を整理する事実アナウンサーです。\n"
            "**禁止事項**: 「○日後に○％上昇する確率」「目標株価」など値動き予測を一切書かないでください。\n"
            "**必須事項**: 全ての主張に出典 URL を併記してください。出典が示せない事実は「(出典確認できず)」と明記してください。\n\n"
            f"# 対象銘柄\n"
            f"- コード: {candidate['code']}\n"
            f"- 名前: {candidate['name']}\n"
            f"- セクター: {candidate.get('sector', '')}\n"
            f"- データ基準日: {candidate.get('data_as_of', '')}\n\n"
            f"# テクニカルシグナル（Python計算済み・新規数値生成禁止）\n{signals_text}\n\n"
            f"# 価格スナップショット\n{snapshot_text}\n\n"
            f"{('# 投資憲法スタイル抜粋' + chr(10) + constitution_excerpt + chr(10) + chr(10)) if constitution_excerpt else ''}"
            "# 出力フォーマット（必ず以下の構造で出力。それ以外は出力しない）\n"
            "## 直近1ヶ月の重要 IR / 適時開示\n"
            "（箇条書き、各項目に発表日と出典 URL を併記。なければ「該当なし」）\n\n"
            "## 直近の決算で確認できる事実\n"
            "（売上・利益・ガイダンス改定の有無のみ。数値は引用元の値を直接参照可、ただし出典 URL 必須）\n\n"
            "## 直近のニュース・センチメント要約\n"
            "（事実ベースの箇条書き、各項目に出典 URL）\n\n"
            "## ポジティブ材料（買い検討材料）\n"
            "## 懸念材料（注意点）\n"
            "## 投資憲法との整合性\n"
            "（合致・不合致を箇条書きで簡潔に）\n"
        )

    @staticmethod
    def build_business_model_prompt(code: str, name: str = "") -> str:
        """宝石7「ビジネスモデル」＋中計KPI/マテリアリティの定性分析プロンプト。
        決算分析の地図（村上茂久）の考え方に沿い、決算書の裏のビジネスモデルと
        企業が重視するKPIを、IR・決算説明資料・中期経営計画・統合報告書から整理させる。"""
        return (
            "あなたは予測者ではなく、企業の公開情報を整理する事実アナウンサーです。\n"
            "**禁止事項**: 目標株価・値動き予測は書かない。\n"
            "**必須事項**: 各主張に出典URLを併記。確認できない事実は「(出典確認できず)」と明記。\n"
            "Web検索で、企業の公式IR・決算説明資料・中期経営計画・統合報告書・有価証券報告書を参照すること。\n\n"
            f"# 対象銘柄\n- コード: {code}\n- 名前: {name}\n\n"
            "# 出力フォーマット（決算分析の地図『7つの宝石』に沿う。以下の構造のみ出力）\n"
            "## ビジネスモデル（宝石7：決算書の裏側）\n"
            "（誰に何を売り、どこで稼いでいるか。収益構造の特徴を簡潔に。出典URL）\n\n"
            "## 企業が重視するKPI（宝石3／中期経営計画）\n"
            "（中計で掲げる財務目標＝売上/営業利益率/ROE/ROIC/EBITDA/FCF等と、事業KPIを箇条書き。目標年度も。出典URL）\n\n"
            "## マテリアリティ／ESGの要点（統合報告書）\n"
            "（重要課題として何を掲げているか。なければ「該当情報なし」）\n\n"
            "## 定性面での買い材料\n"
            "## 定性面での懸念\n"
            "## 総括（テクニカル×ファンダの定量判断を、定性面が補強するか／覆すか）\n"
            "（1〜3行で簡潔に）\n"
        )

    @staticmethod
    def build_phase_c_prompt(style_display: str, results_with_qualitative: list[dict]) -> str:
        """Phase C: 統合レポート生成プロンプト。"""
        lines = [
            f"スタイル: {style_display} のスクリーニング結果を統合レポートにまとめます。",
            "",
            "**禁止事項**: 順位入れ替えや値動き予測は禁止。提示順を維持してください。",
            "**禁止事項**: 「○％上昇」「目標株価」「○日後」など予測表現を一切使わないでください。",
            "",
            "# 入力（各銘柄の Python 計算結果 + 質的補強レポート）",
            "",
        ]
        for i, r in enumerate(results_with_qualitative, 1):
            lines.append(f"## {i}. {r.get('code')} {r.get('name')}（スコア {r.get('score')}）")
            sigs = r.get("signals") or []
            for s in sigs:
                lines.append(f"  - {s['name']}: {s['value']} (基準 {s['threshold']}, "
                             f"{'通過' if s['passed'] else '未通過'})")
            qual = r.get("qualitative") or ""
            if qual:
                lines.append("")
                lines.append("### 質的補強")
                lines.append(qual)
                lines.append("")
        lines += [
            "",
            "# 出力フォーマット",
            "## エグゼクティブサマリー",
            "（このスクリーニング結果全体の傾向を 3 行以内で）",
            "",
            "## 銘柄別 1 行サマリー",
            "（提示順を維持して各銘柄を 1 行で要約。憶測禁止、Python計算結果と質的補強の事実のみを引用）",
            "",
            "## 全体の留意点",
            "（市場環境やセクター集中など、判断時の注意点を箇条書き）",
            "",
            "---",
            "※ 本レポートは投資推奨ではありません。最終的な投資判断は自己責任でお願いします。",
        ]
        return "\n".join(lines)

    @staticmethod
    def sanitize_qualitative_output(text: str) -> tuple[str, list[str]]:
        """ハルシネーション禁止語が混入していないかチェック。

        Returns:
            (text, warnings)  warnings に検出された禁止表現を入れる。
        """
        import re
        warnings: list[str] = []
        forbidden_patterns = [
            (r"\d+\s*[%％]\s*(?:の)?(?:確率|可能性)", "確率予測"),
            (r"\d+\s*日後", "日数予測"),
            (r"目標株価", "目標株価"),
            (r"\d+\s*[%％]?\s*上昇する", "値動き予測"),
        ]
        for pattern, label in forbidden_patterns:
            if re.search(pattern, text):
                warnings.append(label)
        return text, warnings
