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

        needs_fundamentals = style in ("value", "growth")
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
        needs_fundamentals = secondary_style in ("value", "growth")

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
