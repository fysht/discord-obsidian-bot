"""日本株スクリーナー Cog（PWA 専用）。

機械的なスクリーニング (Phase A) と Gemini による質的補強 (Phase B/C) を提供する。
Drive 保存・履歴管理は InvestmentCog のヘルパーを再利用する。
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import re
import secrets
from typing import Optional

from discord.ext import commands

from config import JST
from services.screener_service import ScreenerService
from services.screener_engine import (
    list_strategies, factor_axes_catalog, select_with_sector_cap,
)


def _sel_score(c: dict):
    """候補 dict の選定順位スコア。selection_score（品質×RS の合成）を優先し、
    無ければ score にフォールバック。複数スタイルのマージ並べ替えで使う。"""
    v = c.get("selection_score")
    if v is None:
        v = c.get("score")
    return v if v is not None else 0


import math as _math


def _json_finite(obj):
    """dict/list を再帰的に走査し、NaN/Inf を None に置換して JSON 互換にする
    （ジョブ結果を candidates_json へ保存する前に通す。フロントの JSON.parse 失敗を防ぐ）。"""
    if isinstance(obj, float):
        return obj if _math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_finite(v) for v in obj]
    return obj


GEMINI_FLASH_MODEL = "gemini-2.5-flash"
GEMINI_PRO_MODEL = "gemini-2.5-pro"

# 1 ジョブ内で Gemini に投げる候補数の上限（コスト保護）
MAX_QUALITATIVE_CANDIDATES = 10
# 同時実行可能なジョブ数（コスト保護）
MAX_CONCURRENT_JOBS = 1


class ScreenerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.service = ScreenerService()

    # ==========================================================
    # 同期 API (Phase A: 機械スクリーニング)
    # ==========================================================

    async def list_styles(self) -> dict:
        # styles（上層メソッド）に加え、共通ファクター軸（下層ゲートの語彙）も返す。
        # 各 style の axes で「メソッド＝軸の組み合わせ」が辿れる＝2層モデルの地図。
        return {"ok": True, "styles": list_strategies(), "axes": factor_axes_catalog()}

    async def list_universes(self) -> dict:
        names = await self.service.list_universes()
        # 「all」は CSV が未配置でも UI で選択肢として常に表示する
        # （実行時にエラーで案内し、ユーザーに tools/fetch_jpx_universe.py を促す）
        if "all" not in names:
            names = list(names) + ["all"]
        return {"ok": True, "universes": names}

    async def run_screening(
        self,
        style: str,
        top_n: int = 10,
        universe_name: str = "topix500",
        min_market_cap_jpy: Optional[int] = None,
        exclude_sectors: Optional[list[str]] = None,
        enabled_filters: Optional[list[str]] = None,
        refine: bool = False,
    ) -> dict:
        return await self.service.run_screening(
            style=style,
            top_n=int(top_n),
            universe_name=universe_name,
            min_market_cap_jpy=min_market_cap_jpy,
            exclude_sectors=exclude_sectors,
            enabled_filters=enabled_filters,
            refine=refine,
        )

    async def apply_secondary_style(
        self,
        candidates: list[dict],
        secondary_style: str,
        enabled_filters: Optional[list[str]] = None,
    ) -> dict:
        return await self.service.apply_secondary_style(
            candidates=candidates,
            secondary_style=secondary_style,
            enabled_filters=enabled_filters,
        )

    async def get_ohlcv_series(self, code: str, days: int = 120) -> dict:
        return await self.service.get_ohlcv_series(code, days)

    # ==========================================================
    # 判断の事後検証ループ（売買時の自動記録 → 答え合わせ → 学習）
    # ==========================================================

    async def record_trade_decision(
        self,
        code: str,
        name: str = "",
        market: str = "",
        trade_action: str = "buy",
        price: Optional[float] = None,
        style: str = "",
    ) -> dict:
        """売買成立時に診断スナップショットを記録する（InvestmentCog から呼ばれる）。"""
        return await self.service.record_trade_decision(
            code=code, name=name, market=market,
            trade_action=trade_action, price=price, style=style,
        )

    async def verify_due_decisions(self, force: bool = False) -> dict:
        """検証期日（20/60営業日）を過ぎた判断を答え合わせする。"""
        return await self.service.verify_due_decisions(force=force)

    async def decision_review_report(self, horizon: str = "d60") -> dict:
        """検証済みの判断を集計し、シグナル別などの的中率（学習結果）を返す。"""
        return await self.service.decision_review_report(horizon=horizon)

    async def list_decision_reviews(self, status: Optional[str] = None, limit: int = 200) -> dict:
        """記録済みの判断（事後検証用スナップショット）を一覧する。"""
        from api.database import decision_review_list
        items = await decision_review_list(status=status, limit=limit)
        return {"ok": True, "items": items}

    async def delete_decision_review(self, review_id: int) -> dict:
        from api.database import decision_review_delete
        ok = await decision_review_delete(review_id)
        return {"ok": ok}

    async def analyze_projection(self, code: str, days: int = 750) -> dict:
        return await self.service.analyze_projection(code, days)

    async def backtest_rotation(self, codes: list, days: int = 750, rebalance_days: int = 20,
                                top_k: int = 5, lookback: int = 60) -> dict:
        """与えた銘柄群でローテーション戦略 vs buy&hold をバックテスト（市場別分離・ポート単位）。"""
        return await self.service.backtest_rotation(
            codes, days=days, rebalance_days=rebalance_days, top_k=top_k, lookback=lookback)

    async def backtest_universe(self, universe_name: str = "topix500", days: int = 750,
                                rebalance_days: int = 20, top_k: int = 10,
                                lookback: int = 60, max_codes: int = 300) -> dict:
        """ユニバース全体（構成員）でローテーション戦略 vs buy&hold をバックテスト（本格版）。"""
        return await self.service.backtest_universe(
            universe_name, days=days, rebalance_days=rebalance_days, top_k=top_k,
            lookback=lookback, max_codes=max_codes)

    async def score_all_methods(self, code: str, days: int = 300) -> dict:
        """1銘柄を登録済み全メソッドで採点し、メソッド別の点数と得意メソッドを返す。"""
        return await self.service.score_all_methods(code, days)

    async def advise_portfolio(
        self,
        candidates: Optional[list[dict]] = None,
        days: int = 300,
        holdings: Optional[list[dict]] = None,
        with_financials: bool = False,
        capital: Optional[float] = None,
        hard_stop_pct: float = -0.08,
    ) -> dict:
        """保有銘柄（InvestmentCog のポートフォリオ）＋候補をテクニカル×ファンダで一括診断。
        holdings 未指定なら InvestmentCog から取得する。
        with_financials=True で EDINET 有報の安全性/キャッシュ指標も織り込む。"""
        if holdings is None:
            inv = self.bot.get_cog("InvestmentCog")
            if inv:
                try:
                    pl = await inv.portfolio_list()
                    holdings = (pl or {}).get("holdings") or []
                except Exception:
                    holdings = []
            else:
                holdings = []
        return await self.service.advise_portfolio(
            holdings, candidates=candidates, days=days, with_financials=with_financials,
            capital=capital, hard_stop_pct=hard_stop_pct)

    async def advise_portfolio_full(
        self,
        candidates: Optional[list[dict]] = None,
        days: int = 300,
        holdings: Optional[list[dict]] = None,
        with_financials: bool = False,
        capital: Optional[float] = None,
        hard_stop_pct: float = -0.08,
        auto_screen: bool = False,
    ) -> dict:
        """『毎日ここから一括診断』の本体。auto_screen=True なら全メソッド（JP/US 横断）で
        新規候補を自動抽出してから advise_portfolio を実行し、候補に手法ラベル(matched_styles)を付ける。
        同期エンドポイントとバックグラウンドジョブの双方から共有する共通処理。"""
        cand_list = list(candidates or [])
        matched_by_code: dict = {}
        if auto_screen:
            inv = self.bot.get_cog("InvestmentCog")
            if inv:
                try:
                    gathered = await inv.gather_daily_candidates()
                    matched_by_code = gathered.get("matched_by_code") or {}
                    seen = {str(c.get("code")) for c in cand_list if c.get("code")}
                    for c in (gathered.get("candidates") or []):
                        code = str(c.get("code") or "")
                        if code and code not in seen:
                            seen.add(code)
                            cand_list.append(c)
                except Exception:
                    logging.exception("advise_portfolio_full: gather_daily_candidates failed")
        result = await self.advise_portfolio(
            candidates=cand_list or None, days=days, holdings=holdings,
            with_financials=with_financials, capital=capital, hard_stop_pct=hard_stop_pct)
        # どの手法が拾った候補かをカードで示せるよう、診断結果の候補に手法の表示名を付与する。
        if matched_by_code and isinstance(result, dict):
            for c in (result.get("candidates") or []):
                ms = matched_by_code.get(str(c.get("code") or ""))
                if ms:
                    c["matched_styles"] = [self._style_display(s) for s in ms]
        return result

    async def start_advise_job(
        self,
        candidates: Optional[list[dict]] = None,
        with_financials: bool = False,
        capital: Optional[float] = None,
        hard_stop_pct: float = -0.08,
        auto_screen: bool = False,
    ) -> dict:
        """一括診断をバックグラウンドで起動し job_id を返す。auto_screen の全メソッド走査は
        1〜3分かかり HTTP がタイムアウトするため、ジョブ化して /jobs/{id} でポーリングする。
        完了時に Push 通知。"""
        from api.database import screener_job_count_active, screener_job_create

        active = await screener_job_count_active()
        if active >= MAX_CONCURRENT_JOBS:
            return {"ok": False, "error": f"既に {active} 件のジョブが実行中です。完了をお待ちください。"}

        job_id = f"adv_{datetime.datetime.now(JST).strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
        await screener_job_create(job_id, "advise", 0)

        from utils.async_utils import safe_create_task
        safe_create_task(
            self._run_advise_job(
                job_id, candidates=candidates, with_financials=with_financials,
                capital=capital, hard_stop_pct=hard_stop_pct, auto_screen=auto_screen,
            ),
            name=f"screener_advise_{job_id}",
        )
        return {"ok": True, "job_id": job_id, "status": "queued"}

    async def _run_advise_job(
        self,
        job_id: str,
        candidates: Optional[list[dict]] = None,
        with_financials: bool = False,
        capital: Optional[float] = None,
        hard_stop_pct: float = -0.08,
        auto_screen: bool = False,
    ) -> None:
        from api.database import screener_job_update
        import json as _json
        try:
            await screener_job_update(job_id, status="running")
            result = await self.advise_portfolio_full(
                candidates=candidates, with_financials=with_financials,
                capital=capital, hard_stop_pct=hard_stop_pct, auto_screen=auto_screen,
            )
            payload = _json.dumps(_json_finite(result), ensure_ascii=False, default=str)
            if not isinstance(result, dict) or not result.get("ok"):
                await screener_job_update(
                    job_id, status="error",
                    error=str((result or {}).get("error") or "診断に失敗しました"),
                    candidates_json=payload,
                )
                return
            await screener_job_update(job_id, status="done", candidates_json=payload)
            try:
                from api import notification_service
                nh = len([h for h in (result.get("holdings") or []) if h.get("ok")])
                nc = len([c for c in (result.get("candidates") or []) if c.get("ok")])
                await notification_service.send_push(
                    title="🧭 保有＆候補 一括診断が完了しました",
                    body=f"保有{nh}件・新規候補{nc}件を診断しました。アプリを開いて確認してください。",
                    url=f"/?tab=invest&advise_job={job_id}",
                )
            except Exception as e:
                logging.debug(f"advise job push notify error: {e}")
        except Exception as e:
            logging.exception("advise job failed")
            try:
                await screener_job_update(job_id, status="error", error=str(e))
            except Exception:
                pass

    async def save_advice_as_job(self, result: dict, kind: str = "advise") -> Optional[str]:
        """構造化された一括診断結果を done ジョブとして保存し job_id を返す。
        自動通知（16:15 日次スクリーニング / 12:00 昼チェック）から、その結果を
        『毎日ここから』のカード表示（チャート・注目銘柄追加）で開けるようにするため。
        kind: "advise"(手動) / "daily"(16:15 日次) / "noon"(12:00 昼)。style 列に保存し、
        『前回の結果を見る』が最新の日次スクリーニング結果を引けるようにする。"""
        if not isinstance(result, dict) or not result.get("ok"):
            return None
        import json as _json
        from api.database import screener_job_create, screener_job_update
        job_id = f"adv_{datetime.datetime.now(JST).strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
        style_key = "advise" if kind == "advise" else f"advise:{kind}"
        try:
            await screener_job_create(job_id, style_key, 0)
            await screener_job_update(
                job_id, status="done",
                candidates_json=_json.dumps(_json_finite(result), ensure_ascii=False, default=str),
            )
            return job_id
        except Exception:
            logging.exception("save_advice_as_job failed")
            return None

    async def measure_performance(
        self,
        days: int = 500,
        holdings: Optional[list[dict]] = None,
    ) -> dict:
        """保有ポートフォリオが市場平均をアウトパフォームできているかを測定。
        holdings 未指定なら InvestmentCog から取得する。"""
        if holdings is None:
            inv = self.bot.get_cog("InvestmentCog")
            if inv:
                try:
                    pl = await inv.portfolio_list()
                    holdings = (pl or {}).get("holdings") or []
                except Exception:
                    holdings = []
            else:
                holdings = []
        if not holdings:
            return {"ok": False, "error": "保有銘柄が登録されていません。"}
        return await self.service.measure_performance(holdings, days=days)

    async def analyze_business_model(self, code: str, name: str = "", force: bool = False) -> dict:
        """宝石7「ビジネスモデル」＋中計KPI/マテリアリティの定性分析（単一銘柄・Gemini）。
        結果は自動保存され、次回以降はキャッシュを返す（Geminiコスト節約）。
        force=True で最新を再分析。"""
        code = str(code or "").strip()
        if not code:
            return {"ok": False, "error": "コードを指定してください"}
        from services.screener_service import _research_cache_get, _research_cache_set

        if not force:
            cached = await _research_cache_get("bizmodel", code)  # 定性は陳腐化しにくいのでTTLなし
            if cached and cached.get("report"):
                return {"ok": True, "code": code, "name": cached.get("name") or name,
                        "report": cached["report"], "cached": True,
                        "fetched_at": cached.get("fetched_at")}

        inv = self.bot.get_cog("InvestmentCog")
        if not inv or not getattr(inv, "gemini_client", None):
            return {"ok": False, "error": "Gemini が未設定のため定性分析を実行できません。"}
        prompt = ScreenerService.build_business_model_prompt(code, name)
        try:
            text = await inv._gemini_with_search(prompt, feature_key="investment_screener")
        except Exception as e:
            return {"ok": False, "error": f"定性分析に失敗しました: {e}"}
        if not text:
            return {"ok": False, "error": "定性分析を取得できませんでした。"}
        await _research_cache_set("bizmodel", code, {"report": text, "name": name})
        return {"ok": True, "code": code, "name": name, "report": text, "cached": False}

    async def deep_research(self, code: str, name: str = "", sector: str = "", force: bool = False) -> dict:
        """ディープリサーチ（日次ワークフロー③）。決定論エンジンで『特に強い』と絞った1銘柄を、
        Web検索で網羅的に深掘り（事業・決算事実・中計KPI・競合・追い風逆風・カタリスト・リスク・
        バリュエーション文脈）。点数/目標株価/値動き予測は出さない（点数は④のエンジン側で確定）。
        結果はキャッシュ。force=True で再取得。"""
        code = str(code or "").strip()
        if not code:
            return {"ok": False, "error": "コードを指定してください"}
        from services.screener_service import _research_cache_get, _research_cache_set

        if not force:
            cached = await _research_cache_get("deepresearch", code, ttl_days=7)
            if cached and cached.get("report"):
                return {"ok": True, "code": code, "name": cached.get("name") or name,
                        "report": cached["report"], "warnings": cached.get("warnings", []),
                        "cached": True, "fetched_at": cached.get("fetched_at")}

        inv = self.bot.get_cog("InvestmentCog")
        if not inv or not getattr(inv, "gemini_client", None):
            return {"ok": False, "error": "Gemini が未設定のためディープリサーチを実行できません。"}
        prompt = ScreenerService.build_deep_research_prompt(code, name, sector)
        try:
            text = await inv._gemini_with_search(prompt, feature_key="investment_screener")
        except Exception as e:
            return {"ok": False, "error": f"ディープリサーチに失敗しました: {e}"}
        if not text:
            return {"ok": False, "error": "ディープリサーチを取得できませんでした。"}
        # ハルシネーション禁止語（予測・確率・目標株価）の混入チェック
        text, warnings = ScreenerService.sanitize_qualitative_output(text)
        await _research_cache_set("deepresearch", code, {"report": text, "name": name, "warnings": warnings})
        return {"ok": True, "code": code, "name": name, "report": text,
                "warnings": warnings, "cached": False}

    async def run_multi_screening(
        self,
        styles: list[str],
        top_n: int = 10,
        universe_name: str = "topix500",
        min_market_cap_jpy: Optional[int] = None,
        exclude_sectors: Optional[list[str]] = None,
        filter_overrides: Optional[dict[str, list[str]]] = None,
        combine_mode: str = "any",
        refine: bool = False,
    ) -> dict:
        """複数スタイルを並列実行して結果をマージ。

        combine_mode="any" (OR): いずれかのスタイルに合致した銘柄を返す。
        combine_mode="all" (AND): すべてのスタイルに合致した銘柄のみを返す。
        filter_overrides: {style_name: [enabled_filter_keys, ...], ...}
        refine: 単一スタイル時のみ、1段目通過を EDINET/EDGAR の有報実績で再確認して精度を上げる
                （多スタイルは EDINET 走査がスタイル数ぶん重くなるため無効）。
        """
        import asyncio as _asyncio
        if not styles:
            return {"ok": False, "error": "スタイルを1つ以上指定してください"}

        def _filters_for(style_name: str) -> Optional[list[str]]:
            if filter_overrides is None:
                return None
            return filter_overrides.get(style_name)

        if len(styles) == 1:
            result = await self.run_screening(
                style=styles[0], top_n=top_n,
                universe_name=universe_name,
                min_market_cap_jpy=min_market_cap_jpy,
                exclude_sectors=exclude_sectors,
                enabled_filters=_filters_for(styles[0]),
                refine=refine,
            )
            if result.get("ok"):
                if result.get("candidates"):
                    for c in result["candidates"]:
                        c.setdefault("matched_styles", [styles[0]])
                result["styles"] = [styles[0]]
                result["combine_mode"] = combine_mode
                result["applied_filters_by_style"] = {
                    styles[0]: result.pop("applied_filters", [])
                }
            return result

        tasks = [
            self.run_screening(
                style=s, top_n=top_n * 3,
                universe_name=universe_name,
                min_market_cap_jpy=min_market_cap_jpy,
                exclude_sectors=exclude_sectors,
                enabled_filters=_filters_for(s),
            )
            for s in styles
        ]
        results_list = await _asyncio.gather(*tasks)

        total_scanned = 0
        total_qualified = 0
        applied_filters_by_style: dict[str, list[dict]] = {}
        any_near_miss = False

        # 各スタイルの結果を code → candidate dict にインデックス
        per_style_by_code: list[dict[str, dict]] = []
        ok_styles: list[str] = []
        for style_key, result in zip(styles, results_list):
            if not result.get("ok"):
                per_style_by_code.append({})
                continue
            ok_styles.append(style_key)
            total_scanned = max(total_scanned, result.get("scanned", 0))
            total_qualified += result.get("qualified", 0)
            applied_filters_by_style[style_key] = result.get("applied_filters", [])
            if result.get("used_near_miss"):
                any_near_miss = True
            per_style_by_code.append({c["code"]: c for c in result.get("candidates", [])})

        style_displays = [self._style_display(s) for s in styles]
        executed_at = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

        if combine_mode == "all" and len(ok_styles) > 1:
            # AND（緩和版）: 完全一致が無くてもマッチ数の多い順に top_n まで返す。
            # 厳密な AND だと結果ゼロになりやすく実用性が下がるため、
            # 「マッチしたスタイル数」を第一キー、「合計スコア」を第二キーに並べる。
            all_codes: set[str] = set()
            for d in per_style_by_code:
                all_codes |= set(d.keys())

            merged_and: list[dict] = []
            for code in all_codes:
                style_cands_with_key = [
                    (style_key_i, d[code])
                    for style_key_i, d in zip(styles, per_style_by_code)
                    if code in d
                ]
                if not style_cands_with_key:
                    continue
                matched_styles = [k for k, _ in style_cands_with_key]
                style_cands = [c for _, c in style_cands_with_key]
                base = dict(style_cands[0])
                base["score"] = round(sum(c.get("score", 0) for c in style_cands), 2)
                # 並べ替え用に各スタイルの選定スコア（品質×RS）も合算しておく。
                base["selection_score"] = round(sum(_sel_score(c) for c in style_cands), 2)
                base["signals"] = [sig for c in style_cands for sig in (c.get("signals") or [])]
                base["matched_styles"] = matched_styles
                base["match_count"] = len(matched_styles)
                base["total_styles"] = len(styles)
                # 全スタイルに合致しなければ「near miss 扱い」として扱う
                base["is_near_miss"] = (len(matched_styles) < len(styles)) or any(
                    c.get("is_near_miss") for c in style_cands
                )
                base["failed_filters"] = [f for c in style_cands for f in (c.get("failed_filters") or [])]
                merged_and.append(base)

            # マッチ数を第一キー、選定スコア（品質×RS の合算）を第二キーに並べる。
            merged_and.sort(
                key=lambda c: (c.get("match_count", 0), _sel_score(c)),
                reverse=True,
            )
            all_cands = select_with_sector_cap(merged_and, top_n, max(3, (top_n + 2) // 3))
            combine_label = "AND"
            if all_cands and any(c.get("match_count", 0) < len(styles) for c in all_cands):
                any_near_miss = True
        else:
            # OR: いずれかのスタイルに合致（従来動作）
            merged: dict[str, dict] = {}
            for style_key, by_code in zip(styles, per_style_by_code):
                for code, cand in by_code.items():
                    cand_copy = dict(cand)
                    if code not in merged:
                        cand_copy["matched_styles"] = [style_key]
                        merged[code] = cand_copy
                    else:
                        merged[code]["matched_styles"].append(style_key)
                        if _sel_score(cand_copy) > _sel_score(merged[code]):
                            matched = merged[code]["matched_styles"]
                            cand_copy["matched_styles"] = matched
                            merged[code] = cand_copy
            ranked = sorted(merged.values(), key=_sel_score, reverse=True)
            all_cands = select_with_sector_cap(ranked, top_n, max(3, (top_n + 2) // 3))
            combine_label = "OR"

        data_as_of = all_cands[0].get("data_as_of") if all_cands else datetime.datetime.now(JST).strftime("%Y-%m-%d")

        return {
            "ok": True,
            "styles": styles,
            "style_display": " / ".join(style_displays) + f" [{combine_label}]",
            "combine_mode": combine_mode,
            "universe": universe_name,
            "data_as_of": data_as_of,
            "executed_at": executed_at,
            "scanned": total_scanned,
            "qualified": total_qualified,
            "applied_filters_by_style": applied_filters_by_style,
            "used_near_miss": any_near_miss,
            "candidates": all_cands,
        }

    # ==========================================================
    # 非同期 API (Phase A: 機械スクリーニングをバックグラウンドで)
    # ==========================================================

    async def start_machine_screening(
        self,
        styles: list[str],
        top_n: int = 10,
        universe_name: str = "topix500",
        min_market_cap_jpy: Optional[int] = None,
        exclude_sectors: Optional[list[str]] = None,
        filter_overrides: Optional[dict[str, list[str]]] = None,
        combine_mode: str = "any",
        refine: bool = False,
    ) -> dict:
        """機械スクリーニング (Phase A) をバックグラウンドで実行し job_id を返す。
        全銘柄ユニバースのように時間がかかるケース向け。完了時に Push 通知。"""
        from api.database import screener_job_count_active, screener_job_create
        import json as _json

        if not styles:
            return {"ok": False, "error": "スタイルを1つ以上指定してください"}

        active = await screener_job_count_active()
        if active >= MAX_CONCURRENT_JOBS:
            return {
                "ok": False,
                "error": f"既に {active} 件のジョブが実行中です。完了をお待ちください。",
            }

        style_key = "machine:" + ",".join(styles)
        job_id = f"scrA_{datetime.datetime.now(JST).strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
        # candidates_json にリクエスト条件もメタとして保持しておく（再現用）
        await screener_job_create(job_id, style_key, 0)

        request_meta = {
            "styles": styles,
            "top_n": int(top_n),
            "universe": universe_name,
            "min_market_cap_jpy": min_market_cap_jpy,
            "exclude_sectors": exclude_sectors or [],
            "filter_overrides": filter_overrides or {},
            "combine_mode": combine_mode,
            "refine": bool(refine),
        }
        try:
            from api.database import screener_job_update
            await screener_job_update(job_id, candidates_json=_json.dumps(
                {"_request": request_meta}, ensure_ascii=False,
            ))
        except Exception:
            pass

        from utils.async_utils import safe_create_task
        safe_create_task(
            self._run_machine_screening_job(
                job_id=job_id,
                styles=styles, top_n=top_n, universe_name=universe_name,
                min_market_cap_jpy=min_market_cap_jpy,
                exclude_sectors=exclude_sectors,
                filter_overrides=filter_overrides,
                combine_mode=combine_mode,
                refine=refine,
            ),
            name=f"screener_machine_{job_id}",
        )

        return {
            "ok": True,
            "job_id": job_id,
            "status": "queued",
            "universe": universe_name,
        }

    async def _run_machine_screening_job(
        self,
        job_id: str,
        styles: list[str],
        top_n: int,
        universe_name: str,
        min_market_cap_jpy: Optional[int],
        exclude_sectors: Optional[list[str]],
        filter_overrides: Optional[dict[str, list[str]]],
        combine_mode: str,
        refine: bool = False,
    ) -> None:
        from api.database import screener_job_update
        import json as _json
        try:
            await screener_job_update(job_id, status="running")
            result = await self.run_multi_screening(
                styles=styles,
                top_n=top_n,
                universe_name=universe_name,
                min_market_cap_jpy=min_market_cap_jpy,
                exclude_sectors=exclude_sectors,
                filter_overrides=filter_overrides,
                combine_mode=combine_mode,
                refine=refine,
            )
            payload = _json.dumps(result, ensure_ascii=False, default=str)
            if not result.get("ok"):
                await screener_job_update(
                    job_id, status="error",
                    error=str(result.get("error") or "失敗しました"),
                    candidates_json=payload,
                )
                return
            await screener_job_update(
                job_id, status="done",
                candidates_json=payload,
                progress_current=int(result.get("scanned") or 0),
                progress_total=int(result.get("scanned") or 0),
            )
            # Push 通知
            try:
                from api import notification_service
                n = len(result.get("candidates") or [])
                style_display = result.get("style_display") or " / ".join(styles)
                await notification_service.send_push(
                    title=f"🔎 スクリーニング完了 ({style_display})",
                    body=f"{n} 銘柄が条件を通過しました。アプリを開いて結果を確認してください。",
                    url=f"/?tab=invest&screener_job={job_id}",
                )
            except Exception as e:
                logging.debug(f"machine screening push notify error: {e}")
        except Exception as e:
            logging.exception("machine screening job failed")
            try:
                await screener_job_update(job_id, status="error", error=str(e))
            except Exception:
                pass

    # ==========================================================
    # 非同期 API (Phase B/C: Gemini 質的分析)
    # ==========================================================

    async def start_qualitative_analysis(
        self,
        styles: list[str],
        candidates: list[dict],
        use_pro: bool = False,
    ) -> dict:
        """質的分析ジョブを起動し job_id を返す。実処理はバックグラウンドで実行。"""
        from api.database import screener_job_count_active, screener_job_create

        if not candidates:
            return {"ok": False, "error": "候補が空です"}
        if not styles:
            return {"ok": False, "error": "スタイルが指定されていません"}

        # 同時実行数上限
        active = await screener_job_count_active()
        if active >= MAX_CONCURRENT_JOBS:
            return {"ok": False, "error": f"既に {active} 件のジョブが実行中です。完了をお待ちください。"}

        # コストメーター: 重い処理を抑制
        try:
            from services import cost_meter_service
            if await cost_meter_service.should_throttle_heavy_tasks():
                return {"ok": False, "error": "API 月額閾値を超過しているため、質的分析は一時停止中です。"}
        except Exception:
            pass

        # 上限内に切り詰め
        candidates = candidates[:MAX_QUALITATIVE_CANDIDATES]

        style_key = ",".join(styles)
        job_id = f"scr_{datetime.datetime.now(JST).strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
        await screener_job_create(job_id, style_key, len(candidates))

        from utils.async_utils import safe_create_task
        safe_create_task(
            self._run_qualitative_job(job_id, styles, candidates, use_pro=use_pro),
            name=f"screener_qual_{job_id}",
        )

        model_label = "Pro" if use_pro else "Flash"
        return {
            "ok": True,
            "job_id": job_id,
            "status": "queued",
            "model": model_label,
            "expected_seconds": len(candidates) * (20 if use_pro else 12),
        }

    async def get_job_status(self, job_id: str) -> dict:
        from api.database import screener_job_get
        import json as _json
        job = await screener_job_get(job_id)
        if not job:
            return {"ok": False, "error": "ジョブが見つかりません"}
        # machine スクリーニング（Phase A 非同期）の場合は candidates_json が
        # `run_multi_screening` の全結果なので、そのまま展開してフロントへ返す。
        result_payload = None
        cj = (job.get("candidates_json") or "").strip()
        if cj:
            try:
                parsed = _json.loads(cj)
                # _request はメタ情報。実結果は ok キーで判別
                if isinstance(parsed, dict) and "ok" in parsed:
                    result_payload = parsed
            except Exception:
                result_payload = None
        return {
            "ok": True,
            "status": job["status"],
            "progress": {
                "current": job["progress_current"],
                "total": job["progress_total"],
                "current_ticker": job["current_ticker"],
            },
            "saved_as": job.get("saved_as", ""),
            "report_markdown": job.get("report_markdown", ""),
            "error": job.get("error", ""),
            "result": result_payload,
        }

    async def latest_advice_result(self, kind: str = "daily") -> dict:
        """指定種別の最新 done 一括診断結果を復元して返す（『前回の結果を見る』用）。
        kind="daily" で 16:15 の日次スクリーニング結果を引く。"""
        from api.database import screener_job_latest_done
        import json as _json
        style = "advise" if kind == "advise" else f"advise:{kind}"
        job = await screener_job_latest_done(style)
        if not job:
            return {"ok": False, "error": "保存された結果がありません"}
        cj = (job.get("candidates_json") or "").strip()
        try:
            result = _json.loads(cj) if cj else None
        except Exception:
            result = None
        if not isinstance(result, dict) or not result.get("ok"):
            return {"ok": False, "error": "結果を復元できませんでした"}
        return {"ok": True, "job_id": job.get("job_id"),
                "created_at": job.get("created_at"), "result": result}

    async def _run_qualitative_job(
        self,
        job_id: str,
        styles: list[str],
        candidates: list[dict],
        use_pro: bool = False,
    ) -> None:
        from api.database import screener_job_update
        from services.screener_service import ScreenerService

        try:
            await screener_job_update(job_id, status="running")

            inv_cog = self.bot.get_cog("InvestmentCog")
            if not inv_cog or not inv_cog.gemini_client:
                await screener_job_update(
                    job_id, status="error",
                    error="InvestmentCog または Gemini クライアントが利用できません",
                )
                return

            constitution_excerpt = await self._fetch_style_sections(inv_cog, styles)
            style_key = ",".join(styles)
            style_display = " / ".join(self._style_display(s) for s in styles)

            results_with_qual: list[dict] = []
            if use_pro:
                model_b = GEMINI_PRO_MODEL
            else:
                from services.gemini_model_resolver import resolve_gemini_model
                model_b = await resolve_gemini_model("screener_qualitative", default_pro=False)

            for idx, cand in enumerate(candidates, 1):
                code = cand.get("code", "")
                await screener_job_update(
                    job_id,
                    progress_current=idx - 1,
                    current_ticker=code,
                )
                # 銘柄が複数スタイルにマッチしている場合は該当スタイルの憲法のみ使う
                matched = cand.get("matched_styles") or styles
                if len(matched) < len(styles):
                    cand_excerpt = await self._fetch_style_sections(inv_cog, matched)
                else:
                    cand_excerpt = constitution_excerpt
                prompt = ScreenerService.build_phase_b_prompt(cand, cand_excerpt)
                try:
                    raw = await inv_cog._gemini_with_search(prompt, model=model_b)
                except Exception as e:
                    logging.error(f"screener Phase B error for {code}: {e}")
                    raw = ""
                cleaned, warnings = ScreenerService.sanitize_qualitative_output(raw)
                if warnings:
                    cleaned += f"\n\n> ⚠️ 出力に予測表現が混入していたため要確認: {', '.join(warnings)}"
                cand_with_qual = dict(cand)
                cand_with_qual["qualitative"] = cleaned
                results_with_qual.append(cand_with_qual)

            # Phase C: 統合
            await screener_job_update(
                job_id,
                progress_current=len(candidates),
                current_ticker="統合中",
            )
            model_c = model_b
            phase_c_prompt = ScreenerService.build_phase_c_prompt(style_display, results_with_qual)
            try:
                summary = await inv_cog._gemini_plain(phase_c_prompt, model=model_c)
            except Exception as e:
                logging.error(f"screener Phase C error: {e}")
                summary = ""

            # Markdown レポート組み立て
            report_md = self._build_report_markdown(style_key, style_display, results_with_qual, summary)

            # Drive 保存
            today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
            filename = f"{today}_{_safe_filename(style_key)}.md"
            try:
                from cogs.investment_cog import SCREENINGS_FOLDER
                await inv_cog._save_dated_note(SCREENINGS_FOLDER, filename, report_md)
            except Exception as e:
                logging.error(f"screener save error: {e}")
                filename = ""

            await screener_job_update(
                job_id,
                status="done",
                progress_current=len(candidates),
                current_ticker="",
                report_markdown=report_md,
                saved_as=filename,
            )

            # Push 通知
            try:
                from api import notification_service
                await notification_service.send_push(
                    title=f"🔎 スクリーナー完了 ({style_display})",
                    body=f"{len(candidates)} 銘柄の質的分析が完了しました",
                    url="/?tab=invest",
                )
            except Exception as e:
                logging.debug(f"push notify error: {e}")

        except Exception as e:
            logging.exception("screener qualitative job failed")
            await screener_job_update(job_id, status="error", error=str(e))

    @staticmethod
    def _style_display(style: str) -> str:
        for s in list_strategies():
            if s["name"] == style:
                return s["display_name"]
        return style

    @staticmethod
    async def _fetch_style_section(inv_cog, style: str) -> str:
        """投資憲法から1スタイルのセクションを抽出。"""
        return await ScreenerCog._fetch_style_sections(inv_cog, [style])

    @staticmethod
    async def _fetch_style_sections(inv_cog, styles: list[str]) -> str:
        """投資憲法から複数スタイルのセクションを抽出して結合する。"""
        try:
            content = await inv_cog._read_constitution()
        except Exception:
            return ""
        if not content:
            return ""
        try:
            from utils.constitution_parser import parse_constitution
            parsed = parse_constitution(content)
            common = parsed.get("common") or ""
            style_map = parsed.get("styles") or {}
            parts = [common] if common else []
            for style in styles:
                if style in style_map:
                    body = style_map[style].get("body", "")
                    if body:
                        display = style_map[style].get("title") or style
                        parts.append(f"### スタイル: {display}\n{body}")
            if parts:
                return "\n\n".join(parts).strip()
            return common.strip() or content[:2000]
        except Exception:
            return content[:2000]

    @staticmethod
    def _build_report_markdown(style: str, style_display: str, candidates: list[dict], summary: str) -> str:
        today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
        lines = [
            f"# 銘柄スクリーニング結果 — {style_display}",
            "",
            f"- スタイル: `{style}`",
            f"- 実行日: {today}",
            f"- 対象銘柄数: {len(candidates)}",
            "",
            "---",
            "",
            "## 統合サマリー",
            "",
            summary or "_(サマリー生成に失敗しました)_",
            "",
            "---",
            "",
            "## 銘柄詳細",
            "",
        ]
        for i, c in enumerate(candidates, 1):
            lines.append(f"### {i}. {c.get('code')} {c.get('name')}（スコア {c.get('score')}）")
            lines.append("")
            ps = c.get("price_snapshot") or {}
            if ps:
                lines.append(f"- 終値: {ps.get('close')} / 前日比 {ps.get('change_pct')}%")
                lines.append(f"- 52週レンジ: {ps.get('low_52w')} 〜 {ps.get('high_52w')}")
                lines.append(f"- データ基準日: {c.get('data_as_of', '')}")
                lines.append("")
            lines.append("**テクニカルシグナル:**")
            for s in c.get("signals", []):
                mark = "✅" if s["passed"] else "❌"
                lines.append(f"- {mark} {s['name']}: {s['value']} (基準 {s['threshold']})")
            lines.append("")
            qual = c.get("qualitative") or ""
            if qual:
                lines.append("**質的補強:**")
                lines.append("")
                lines.append(qual)
                lines.append("")
            lines.append("---")
            lines.append("")
        lines.append("> ⚠️ 本レポートは投資推奨ではありません。最終的な投資判断は自己責任でお願いします。")
        return "\n".join(lines)


def _safe_filename(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|]", "_", name).strip() or "untitled"


async def setup(bot: commands.Bot):
    await bot.add_cog(ScreenerCog(bot))
