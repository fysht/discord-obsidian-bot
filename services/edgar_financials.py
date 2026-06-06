"""SEC EDGAR から米国株の財務サマリー（自己資本比率・FCF・CF型）を取得する。

EDINET(日本株)の edinet_financials と同じ返り値形状を返し、engine の
evaluate_safety / analyze_position にそのまま流せる。米国株版の安全性/キャッシュ層。

- SEC EDGAR は無料・APIキー不要（ただし User-Agent ヘッダが必須）。
  * ティッカー→CIK: https://www.sec.gov/files/company_tickers.json
  * 企業の全XBRL値: https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json
- 環境変数 SEC_USER_AGENT で連絡先入り UA を設定推奨（未設定でも既定値で動く）。

パース処理（parse_companyfacts）は純粋関数にして合成データで単体テスト可能にする。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

from services.edinet_financials import _cs_pattern_label

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# us-gaap 概念名（優先順に試す）
_CONCEPTS = {
    "total_assets": ["Assets"],
    "equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "operating_cf": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "investing_cf": [
        "NetCashProvidedByUsedInInvestingActivities",
        "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
    ],
    "financing_cf": [
        "NetCashProvidedByUsedInFinancingActivities",
        "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
    ],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ],
}

_CIK_MAP: Optional[dict] = None


def _user_agent() -> str:
    return os.getenv(
        "SEC_USER_AGENT",
        "MyDiscordBot research (contact: set SEC_USER_AGENT env)",
    )


def _latest_annual(us_gaap: dict, concept_names: list[str]):
    """指定概念の最新「年次(10-K)」値と期末日を返す。(値, 期末日) or (None, None)。"""
    for name in concept_names:
        c = us_gaap.get(name)
        if not c:
            continue
        units = c.get("units") or {}
        arr = units.get("USD")
        if not arr:
            # USD 以外しか無い場合は最初のユニットを使う
            arr = next(iter(units.values()), [])
        annual = [x for x in arr if str(x.get("form", "")).startswith("10-K")]
        cands = annual or arr
        cands = [x for x in cands if x.get("val") is not None and x.get("end")]
        if not cands:
            continue
        best = max(cands, key=lambda x: (x.get("end", ""), x.get("fy") or 0))
        try:
            return float(best["val"]), best.get("end")
        except (TypeError, ValueError):
            continue
    return None, None


def parse_companyfacts(facts_json: dict) -> dict:
    """SEC companyfacts JSON から安全性/キャッシュのサマリーを抽出する。純粋関数。

    返り値は edinet_financials.extract_financial_summary と同じ形状。
    """
    us = (facts_json or {}).get("facts", {}).get("us-gaap", {})
    if not us:
        return {"ok": False}

    vals = {k: _latest_annual(us, names)[0] for k, names in _CONCEPTS.items()}
    _, eq_end = _latest_annual(us, _CONCEPTS["equity"])
    _, assets_end = _latest_annual(us, _CONCEPTS["total_assets"])

    assets = vals.get("total_assets")
    equity = vals.get("equity")
    ocf = vals.get("operating_cf")
    icf = vals.get("investing_cf")
    ni = vals.get("net_income")

    equity_ratio = (equity / assets) if (equity is not None and assets and assets != 0) else None
    roe = (ni / equity) if (ni is not None and equity and equity != 0) else None
    fcf = (ocf + icf) if (ocf is not None and icf is not None) else None

    return {
        "ok": any(v is not None for v in vals.values()),
        "equity_ratio": equity_ratio,
        "roe": roe,
        "net_assets": equity,
        "total_assets": assets,
        "operating_cf": ocf,
        "investing_cf": icf,
        "financing_cf": vals.get("financing_cf"),
        "fcf": fcf,
        "net_income": ni,
        "revenue": vals.get("revenue"),
        "cs_pattern": _cs_pattern_label(ocf, icf, vals.get("financing_cf")),
        "period_end": eq_end or assets_end,
        "source": "EDGAR",
    }


# ---------------- ネットワーク I/O ----------------

async def _get_cik_map() -> dict:
    """ティッカー(大文字)→ゼロ詰め10桁CIK のマップを取得（プロセス内キャッシュ）。"""
    global _CIK_MAP
    if _CIK_MAP is not None:
        return _CIK_MAP
    import aiohttp

    headers = {"User-Agent": _user_agent()}
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(SEC_TICKERS_URL) as resp:
                if resp.status != 200:
                    logging.warning(f"SEC tickers HTTP {resp.status}")
                    return {}
                data = json.loads(await resp.text())
    except Exception as e:
        logging.debug(f"SEC tickers 取得失敗: {e}")
        return {}

    m = {}
    for row in (data or {}).values():
        t = str(row.get("ticker", "")).upper().strip()
        cik = row.get("cik_str")
        if t and cik is not None:
            m[t] = f"{int(cik):010d}"
    _CIK_MAP = m
    return m


async def get_financials_for_codes(codes: list[str], days: int = 0) -> dict[str, dict]:
    """米国株ティッカー群の最新年次財務サマリーを SEC EDGAR から取得する。
    返り値: {ticker: summary}（取得できたものだけ）。days は API 互換のための未使用引数。"""
    import aiohttp

    tickers = [str(c).strip().upper() for c in codes if str(c).strip()]
    if not tickers:
        return {}
    cik_map = await _get_cik_map()
    if not cik_map:
        return {}

    headers = {"User-Agent": _user_agent()}
    sem = asyncio.Semaphore(4)
    out: dict[str, dict] = {}

    timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async def fetch(ticker: str):
            cik = cik_map.get(ticker)
            if not cik:
                return
            url = SEC_FACTS_URL.format(cik=cik)
            async with sem:
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            return
                        data = json.loads(await resp.text())
                except Exception as e:
                    logging.debug(f"EDGAR companyfacts 失敗 {ticker}: {e}")
                    return
            summary = parse_companyfacts(data)
            if summary.get("ok"):
                summary["filer_name"] = data.get("entityName")
                # 呼び出し側は元のコード表記で参照するため両方のキーで返す
                out[ticker] = summary

        await asyncio.gather(*(fetch(t) for t in tickers))
    return out
