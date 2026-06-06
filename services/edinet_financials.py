"""EDINET CSV(type=5) から財務サマリー（自己資本比率・FCF・キャッシュフロー等）を抽出する。

決算分析の地図（村上茂久）Phase 3 の「会計×ファイナンス」定量強化のためのデータ源。
yfinance の .info では取れない安全性/キャッシュ指標を、金融庁 EDINET の公式 XBRL-CSV
（有価証券報告書）から正確に取得する。EDINET_API_KEY は既存の edinet_service と共有。

設計方針:
  - パース処理（parse_edinet_csv_text / extract_financial_summary）は純粋関数にして
    合成データで単体テスト可能にする。
  - ネットワーク I/O（find_latest_annual_csv / get_financials_for_codes）は薄く分離。
  - 取れない値は None。落とさない。
"""
from __future__ import annotations

import asyncio
import datetime
import io
import logging
import zipfile
from typing import Optional

from config import JST

# 有報「主要な経営指標等の推移」(SummaryOfBusinessResults) の XBRL 要素 ID 末尾。
# 名前空間プレフィックス（jpcrp_cor: 等）が前置されるので suffix 一致で拾う。
_SUMMARY_TAGS = {
    "equity_ratio": "EquityToAssetRatioSummaryOfBusinessResults",
    "roe": "RateOfReturnOnEquitySummaryOfBusinessResults",
    "net_assets": "NetAssetsSummaryOfBusinessResults",
    "total_assets": "TotalAssetsSummaryOfBusinessResults",
    "operating_cf": "CashFlowsFromUsedInOperatingActivitiesSummaryOfBusinessResults",
    "investing_cf": "CashFlowsFromUsedInInvestmentActivitiesSummaryOfBusinessResults",
    "financing_cf": "CashFlowsFromUsedInFinancingActivitiesSummaryOfBusinessResults",
    "net_income": "ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults",
    "revenue": "NetSalesSummaryOfBusinessResults",
}

_HEADER_KEYS = {
    "element": ("要素ID", "ElementID"),
    "context": ("コンテキストID", "ContextID"),
    "rel_year": ("相対年度",),
    "consolidated": ("連結・個別",),
    "value": ("値", "Value"),
}


def _to_float(s) -> Optional[float]:
    if s is None:
        return None
    t = str(s).strip().replace(",", "")
    if t in ("", "－", "-", "—", "NA", "N/A", "#N/A"):
        return None
    # 括弧表記の負値 (1,234) → -1234
    neg = t.startswith("(") and t.endswith(")")
    if neg:
        t = t[1:-1]
    try:
        f = float(t)
    except ValueError:
        return None
    return -f if neg else f


def parse_edinet_csv_text(text: str) -> list[dict]:
    """EDINET CSV(タブ区切り)テキストを行 dict のリストにする。純粋関数。

    返り値各要素: {element, context, rel_year, consolidated, value(raw str)}
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    header = lines[0].split("\t")
    # ヘッダ名 → 列インデックス
    col = {}
    for key, names in _HEADER_KEYS.items():
        for i, h in enumerate(header):
            if h.strip() in names:
                col[key] = i
                break
    if "element" not in col or "value" not in col:
        return []
    rows = []
    for ln in lines[1:]:
        parts = ln.split("\t")

        def _get(k):
            i = col.get(k)
            return parts[i].strip() if (i is not None and i < len(parts)) else ""

        rows.append({
            "element": _get("element"),
            "context": _get("context"),
            "rel_year": _get("rel_year"),
            "consolidated": _get("consolidated"),
            "value": _get("value"),
        })
    return rows


def _select_current(rows: list[dict], tag_suffix: str) -> Optional[float]:
    """当期・連結優先で 1 値を選ぶ。連結が無ければ個別、当期が無ければ最初の数値。"""
    cands = [r for r in rows if r.get("element", "").endswith(tag_suffix)]
    if not cands:
        return None

    def _is_current(r):
        return ("当期" in (r.get("rel_year") or "")) or ("CurrentYear" in (r.get("context") or ""))

    def _is_consolidated(r):
        c = r.get("consolidated") or ""
        ctx = r.get("context") or ""
        return ("連結" in c) or ("NonConsolidated" not in ctx and "_NonConsolidated" not in ctx)

    for pred in (
        lambda r: _is_current(r) and _is_consolidated(r),
        lambda r: _is_current(r),
        lambda r: _is_consolidated(r),
        lambda r: True,
    ):
        for r in cands:
            if pred(r):
                v = _to_float(r.get("value"))
                if v is not None:
                    return v
    return None


def _cs_pattern_label(o: Optional[float], i: Optional[float], f: Optional[float]) -> Optional[str]:
    """営業/投資/財務 CF の符号からキャッシュフロー 8 パターンを判定（決算分析の地図 3章）。"""
    if o is None or i is None or f is None:
        return None
    key = (o >= 0, i >= 0, f >= 0)
    return {
        (True, False, False): "安定型（本業で稼ぎ投資・返済）",
        (True, False, True): "積極投資型（稼ぎ＋調達で投資）",
        (True, True, False): "回収・改善型（資産売却し返済）",
        (True, True, True): "余剰・再構築準備型",
        (False, True, True): "勝負/スタートアップ型（赤字を調達と売却で）",
        (False, True, False): "リストラ型（資産売却で延命）",
        (False, False, True): "急成長/先行投資型（赤字でも調達し投資）",
        (False, False, False): "大幅見直し・危険水域",
    }.get(key)


def extract_financial_summary(rows: list[dict]) -> dict:
    """パース済み行から、安全性/キャッシュの定量サマリーを組み立てる。純粋関数。"""
    vals = {k: _select_current(rows, suf) for k, suf in _SUMMARY_TAGS.items()}

    eq = vals.get("equity_ratio")
    if eq is not None and abs(eq) > 1.5:  # % 表記なら比率へ
        eq = eq / 100.0
    roe = vals.get("roe")
    if roe is not None and abs(roe) > 1.5:
        roe = roe / 100.0

    ocf = vals.get("operating_cf")
    icf = vals.get("investing_cf")
    fcf_val = (ocf + icf) if (ocf is not None and icf is not None) else None

    return {
        "ok": any(v is not None for v in vals.values()),
        "equity_ratio": eq,
        "roe": roe,
        "net_assets": vals.get("net_assets"),
        "total_assets": vals.get("total_assets"),
        "operating_cf": ocf,
        "investing_cf": icf,
        "financing_cf": vals.get("financing_cf"),
        "fcf": fcf_val,
        "net_income": vals.get("net_income"),
        "revenue": vals.get("revenue"),
        "cs_pattern": _cs_pattern_label(ocf, icf, vals.get("financing_cf")),
        "source": "EDINET",
    }


def extract_from_zip(zip_bytes: bytes) -> dict:
    """EDINET type=5 の ZIP(複数 CSV) を読み、有報本体 CSV から財務サマリーを抽出する。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except Exception as e:
        logging.debug(f"EDINET CSV zip 展開失敗: {e}")
        return {"ok": False}
    all_rows: list[dict] = []
    for nm in zf.namelist():
        if not nm.lower().endswith(".csv"):
            continue
        # 有報本体は jpcrp で始まる。それ以外（監査報告書等）も混ざるが拾って害はない。
        try:
            raw = zf.read(nm)
        except Exception:
            continue
        text = None
        for enc in ("utf-16", "utf-16-le", "utf-8-sig", "cp932"):
            try:
                text = raw.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if not text:
            continue
        all_rows.extend(parse_edinet_csv_text(text))
    if not all_rows:
        return {"ok": False}
    return extract_financial_summary(all_rows)


# ---------------- ネットワーク I/O ----------------

async def find_latest_annual_csv(codes: set[str], days: int = 450) -> dict[str, dict]:
    """指定証券コード群について、過去 days 日の EDINET から最新の有価証券報告書(CSVあり)を
    1 回の日付走査でまとめて見つける。返り値: {sec4: {doc_id, period_end, submit}}。"""
    from services import edinet_service

    api_key = edinet_service.get_api_key()
    if not api_key:
        return {}
    sec_set = {edinet_service._normalize_sec_code(c) for c in codes}
    sec_set.discard("")
    if not sec_set:
        return {}

    import aiohttp

    days = max(1, min(int(days or 450), 1500))
    today = datetime.datetime.now(JST).date()
    dates = [today - datetime.timedelta(days=i) for i in range(days)]
    sem = asyncio.Semaphore(8)
    best: dict[str, dict] = {}

    async with aiohttp.ClientSession() as session:
        async def scan(d):
            async with sem:
                try:
                    results = await edinet_service.list_documents_for_date(d, session, api_key)
                except Exception:
                    return
            for r in results:
                if r.get("docTypeCode") != "120":  # 有価証券報告書のみ
                    continue
                if not r.get("csvFlag"):
                    continue
                sec4 = (r.get("secCode") or "")[:4]
                if sec4 not in sec_set:
                    continue
                submit = r.get("submitDateTime") or ""
                cur = best.get(sec4)
                if cur is None or submit > cur.get("submit", ""):
                    best[sec4] = {
                        "doc_id": r.get("docID"),
                        "period_end": r.get("periodEnd"),
                        "submit": submit,
                        "filer_name": r.get("filerName"),
                    }

        await asyncio.gather(*(scan(d) for d in dates))
    return best


async def get_financials_for_codes(codes: list[str], days: int = 450) -> dict[str, dict]:
    """証券コード群の最新有報から、安全性/キャッシュ財務サマリーを取得する。
    返り値: {code: summary}（取得できたものだけ）。EDINET 走査が重いので候補は少数前提。"""
    from services import edinet_service

    code_list = [str(c).strip() for c in codes if str(c).strip()]
    if not code_list:
        return {}
    docs = await find_latest_annual_csv(set(code_list), days=days)
    if not docs:
        return {}

    sem = asyncio.Semaphore(4)
    out: dict[str, dict] = {}

    async def fetch(code: str):
        sec4 = edinet_service._normalize_sec_code(code)
        doc = docs.get(sec4)
        if not doc:
            return
        async with sem:
            try:
                blob = await edinet_service.download_document(doc["doc_id"], doc_type=5)
            except Exception as e:
                logging.debug(f"EDINET CSV download 失敗 {code}: {e}")
                blob = None
        if not blob:
            return
        summary = extract_from_zip(blob)
        if summary.get("ok"):
            summary["doc_id"] = doc.get("doc_id")
            summary["period_end"] = doc.get("period_end")
            summary["filer_name"] = doc.get("filer_name")
            out[str(code)] = summary

    await asyncio.gather(*(fetch(c) for c in code_list))
    return out
