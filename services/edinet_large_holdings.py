"""EDINET 大量保有報告書（5%ルール）から「カタリスト」シグナルを抽出する。

木原直哉/エミン『確率思考で市場を制する最強の投資術』の「大株主が買い増している＝
上昇のサイン」「物言う株主（アクティビスト）が入っている＝カタリスト」を、金融庁 EDINET の
大量保有報告書（docTypeCode 350）・訂正大量保有報告書（360）から決定論的に取得する。

EDINET では大量保有報告書の `secCode` は発行会社（投資対象）、`filerName` は保有者（提出者）。
よって secCode で対象企業の被提出書類を拾い、filerName で保有者を判定できる。

設計方針（edinet_financials.py と同じ）:
  - パース（parse_large_holding_rows / summarize_filings）は純粋関数にして合成データで単体テスト可能に。
  - ネットワーク I/O（get_large_holdings_for_code）は薄く分離。EDINET 走査は重いので単一銘柄前提。
  - 取れない値は None / 空。落とさない。EDINET_API_KEY が無ければ {"ok": False} を即返す。
"""
from __future__ import annotations

import asyncio
import datetime
import io
import logging
import zipfile
from typing import Optional

from config import JST
from services.edinet_financials import parse_edinet_csv_text, _to_float

# 大量保有報告書 系の docTypeCode（EDINET 公式）
LARGE_HOLDING_DOC_TYPES = {"350", "360"}  # 350=大量保有報告書 / 360=訂正大量保有報告書

# 大量保有 XBRL（jplvh 名前空間）の要素 ID 末尾。名前空間プレフィックスは前置されるので suffix 一致で拾う。
_RATIO_NOW_SUFFIX = "HoldingRatioOfShareCertificatesEtc"
# 直前の提出書類における保有割合（変更報告書で買い増し/売却を判定するのに使う）
_RATIO_PREV_HINTS = ("LastReport", "PreviousReport", "Previous", "BeforeReport")

# 物言う株主（アクティビスト）として知られる保有者名の断片。filerName 部分一致で判定。
_ACTIVIST_HINTS = (
    "村上", "シティインデックス", "レノ", "南青山不動産", "エスグラント",
    "ストラテジックキャピタル", "オアシス", "エフィッシモ", "スリーディー", "3D",
    "タワー投資", "アスリード", "ダルトン", "ナナホシ", "カタリスト投資",
    "ニッポンアクティブバリュー", "いちごアセット", "ＲＭＢ", "RMB",
    "エリオット", "ファラロン", "バリューアクト", "サウスポイント",
    "ひびき", "シルチェスター", "光通信", "みさき", "ストラテジック",
)


def is_activist(name: Optional[str]) -> bool:
    """保有者名がアクティビスト（物言う株主）らしいか。"""
    if not name:
        return False
    n = str(name)
    return any(h in n for h in _ACTIVIST_HINTS)


def _pct(v: Optional[float]) -> Optional[float]:
    """保有割合を比率(0-1)に正規化。% 表記(>1.5)なら /100。"""
    if v is None:
        return None
    return v / 100.0 if abs(v) > 1.5 else v


def parse_large_holding_rows(rows: list[dict]) -> dict:
    """パース済み CSV 行から、現在/直前の保有割合を取り出す。純粋関数。

    返り値: {"ratio": float|None, "prev_ratio": float|None}（いずれも比率 0-1）。
    """
    ratio = prev = None
    for r in rows:
        el = r.get("element", "")
        if _RATIO_NOW_SUFFIX not in el:  # 大量保有の保有割合系要素のみ
            continue
        v = _pct(_to_float(r.get("value")))
        if v is None:
            continue
        if any(h in el for h in _RATIO_PREV_HINTS):  # 直前提出書類の保有割合
            if prev is None:
                prev = v
        else:
            if ratio is None or v > ratio:  # 当該報告書の保有割合（最大の妥当値）
                ratio = v
    return {"ratio": ratio, "prev_ratio": prev}


def extract_holding_from_zip(zip_bytes: bytes) -> dict:
    """EDINET type=5 ZIP（大量保有報告書 CSV）から保有割合を抽出する。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except Exception as e:
        logging.debug(f"EDINET 大量保有 zip 展開失敗: {e}")
        return {"ratio": None, "prev_ratio": None}
    all_rows: list[dict] = []
    for nm in zf.namelist():
        if not nm.lower().endswith(".csv"):
            continue
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
    return parse_large_holding_rows(all_rows)


def summarize_filings(filings: list[dict]) -> dict:
    """大量保有の提出明細リストから、カタリスト・サマリーを組み立てる。純粋関数。

    filings 各要素: {holder, submit, doc_type, ratio, prev_ratio, is_activist}
    返り値: {ok, count, holders, activist_present, accumulating, latest_ratio, latest_holder, note}
    """
    if not filings:
        return {"ok": False, "count": 0, "holders": [], "activist_present": False,
                "accumulating": False, "latest_ratio": None, "latest_holder": None,
                "note": "直近の大量保有報告書なし"}

    ordered = sorted(filings, key=lambda f: f.get("submit") or "", reverse=True)
    holders = []
    for f in ordered:
        h = f.get("holder")
        if h and h not in holders:
            holders.append(h)
    activist_present = any(f.get("is_activist") for f in ordered)
    # 買い増し: 直近のいずれかで保有割合が前回比プラス
    accumulating = any(
        (f.get("ratio") is not None and f.get("prev_ratio") is not None
         and f["ratio"] > f["prev_ratio"] + 1e-9)
        for f in ordered
    )
    latest = next((f for f in ordered if f.get("ratio") is not None), ordered[0])
    latest_ratio = latest.get("ratio")
    latest_holder = latest.get("holder")

    bits = [f"直近{len(ordered)}件の大量保有報告"]
    if activist_present:
        bits.append("物言う株主あり")
    if accumulating:
        bits.append("買い増し検出")
    if latest_ratio is not None:
        bits.append(f"直近保有 {latest_ratio * 100:.1f}%（{latest_holder or '保有者不明'}）")
    return {
        "ok": True, "count": len(ordered), "holders": holders[:8],
        "activist_present": activist_present, "accumulating": accumulating,
        "latest_ratio": latest_ratio, "latest_holder": latest_holder,
        "note": "／".join(bits),
    }


# ---------------- ネットワーク I/O ----------------

async def get_large_holdings_for_code(code: str, days: int = 180, max_parse: int = 3) -> dict:
    """単一証券コードについて、過去 days 日の大量保有報告書を集めてカタリスト・サマリーを返す。

    メタデータ（保有者名・提出日・アクティビスト判定）は走査だけで得られる。買い増し判定に必要な
    保有割合は重いので直近 max_parse 件だけ CSV をダウンロードして best-effort でパースする。
    EDINET 走査が重いので単一銘柄前提。
    """
    from services import edinet_service

    api_key = edinet_service.get_api_key()
    if not api_key:
        return {"ok": False, "reason": "EDINET_API_KEY 未設定"}
    sec4 = edinet_service._normalize_sec_code(code)
    if not sec4 or len(sec4) != 4:
        return {"ok": False, "reason": "4桁の証券コードが必要"}

    import aiohttp

    days = max(1, min(int(days or 180), 730))
    today = datetime.datetime.now(JST).date()
    dates = [today - datetime.timedelta(days=i) for i in range(days)]
    sem = asyncio.Semaphore(8)
    matched: list[dict] = []

    async with aiohttp.ClientSession() as session:
        async def scan(d):
            async with sem:
                try:
                    results = await edinet_service.list_documents_for_date(d, session, api_key)
                except Exception:
                    return
            for r in results:
                dtc = r.get("docTypeCode") or ""
                desc = r.get("docDescription") or ""
                if dtc not in LARGE_HOLDING_DOC_TYPES and "大量保有" not in desc:
                    continue
                if (r.get("secCode") or "")[:4] != sec4:
                    continue
                matched.append({
                    "doc_id": r.get("docID"),
                    "holder": r.get("filerName"),
                    "submit": r.get("submitDateTime") or "",
                    "doc_type": dtc,
                    "csv": bool(r.get("csvFlag")),
                    "is_activist": is_activist(r.get("filerName")),
                    "ratio": None,
                    "prev_ratio": None,
                })

        await asyncio.gather(*(scan(d) for d in dates))

    if not matched:
        return summarize_filings([])

    matched.sort(key=lambda f: f.get("submit") or "", reverse=True)

    # 直近 max_parse 件だけ CSV をパースして保有割合を埋める（best-effort）
    to_parse = [f for f in matched if f.get("csv")][:max(0, int(max_parse))]
    psem = asyncio.Semaphore(3)

    async def fill(f):
        async with psem:
            try:
                blob = await edinet_service.download_document(f["doc_id"], doc_type=5)
            except Exception as e:
                logging.debug(f"大量保有 CSV download 失敗 {f.get('doc_id')}: {e}")
                blob = None
        if blob:
            h = extract_holding_from_zip(blob)
            f["ratio"] = h.get("ratio")
            f["prev_ratio"] = h.get("prev_ratio")

    await asyncio.gather(*(fill(f) for f in to_parse))

    summary = summarize_filings(matched)
    summary["window_days"] = days
    summary["filings"] = [
        {"holder": f["holder"], "submit": (f["submit"] or "")[:10], "doc_type": f["doc_type"],
         "ratio": f["ratio"], "prev_ratio": f["prev_ratio"], "is_activist": f["is_activist"]}
        for f in matched[:8]
    ]
    return summary
