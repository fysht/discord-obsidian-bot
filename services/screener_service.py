"""日本株スクリーニング・サービス層。

ユニバースに対して並列でデータを取得し、戦略でスコアリングして上位 N を返す。
Gemini 質的分析（Phase B/C）も提供する。
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Optional

from config import JST
from services.jp_stock_data_service import StockDataProvider, get_provider
from services.screener_engine import (
    ScreeningResult,
    STRATEGY_REGISTRY,
    get_strategy,
    list_strategies,
)


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
        from services.screener_engine import analyze_position, compute_relative_metrics

        holdings = holdings or []
        candidates = candidates or []
        held_codes = {str(h.get("code")) for h in holdings if h.get("code")}
        days = max(120, min(int(days or 300), 1000))
        sem = asyncio.Semaphore(4)

        # 財務サマリー（任意・重い処理）。日本株=EDINET、米国株=SEC EDGAR で取得。
        financials_by_code: dict[str, dict] = {}
        if with_financials:
            all_codes = [str(h.get("code")) for h in holdings if h.get("code")]
            all_codes += [str(c.get("code")) for c in candidates
                          if c.get("code") and str(c.get("code")) not in held_codes]
            jp_codes = [c for c in all_codes if c.isdigit()]
            us_codes = [c for c in all_codes if not c.isdigit()]
            if jp_codes:
                try:
                    from services.edinet_financials import get_financials_for_codes as _edinet
                    financials_by_code.update(await _edinet(jp_codes))
                except Exception as e:
                    logging.debug(f"advise EDINET財務取得エラー: {e}")
            if us_codes:
                try:
                    from services.edgar_financials import get_financials_for_codes as _edgar
                    fin_us = await _edgar(us_codes)
                    # EDGAR は大文字ティッカーで返すため、元コード表記にもマッピング
                    for c in us_codes:
                        s = fin_us.get(c.upper())
                        if s:
                            financials_by_code[c] = s
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
        buys = [r for r in candidates_out
                if r.get("ok") and r["verdict"]["action"] == "BUY"]
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
        as_of = next((r.get("as_of") for r in ok_all if r.get("as_of")), "")
        summary = (f"保有{len(holdings_out)}銘柄: 継続{len(keep)}・縮小/売却{len(sells)}。"
                   f"新規候補{len(candidates_out)}銘柄中、両方で買い{len(buys)}件。"
                   f"入替提案{len(rotations)}件。")

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
