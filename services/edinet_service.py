"""EDINET API v2 クライアント。

金融庁 EDINET (https://disclosure.edinet-fsa.go.jp/) の公式 API v2 を呼んで
日本の上場企業の決算関連書類（有価証券報告書・四半期報告書 など）の一覧取得と
PDF ダウンロードを行う。

環境変数 `EDINET_API_KEY` が必須（2024年4月以降、API 利用に登録キーが必須）。
公式: https://api.edinet-fsa.go.jp/api/v2/documents.json
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import os
from typing import Optional

import aiohttp

from config import JST

EDINET_API_BASE = "https://api.edinet-fsa.go.jp/api/v2"

# 取得対象としたい主な書類タイプ（有報・四半期報告書・半期報告書）
EARNINGS_DOC_TYPES = {
    "120": "有価証券報告書",
    "140": "四半期報告書",
    "160": "半期報告書",
}

# 全主要書類タイプ（参考）
DOC_TYPE_LABELS = {
    "010": "有価証券通知書",
    "020": "変更通知書",
    "030": "有価証券届出書",
    "040": "訂正有価証券届出書",
    "050": "発行登録書",
    "060": "訂正発行登録書",
    "070": "発行登録追補書類",
    "080": "発行登録取下げ届出書",
    "090": "発行登録通知書",
    "100": "訂正発行登録通知書",
    "110": "有価証券届出書",
    "120": "有価証券報告書",
    "130": "訂正有価証券報告書",
    "135": "確認書",
    "140": "四半期報告書",
    "150": "訂正四半期報告書",
    "160": "半期報告書",
    "170": "訂正半期報告書",
    "180": "臨時報告書",
    "190": "訂正臨時報告書",
}


def get_api_key() -> Optional[str]:
    return os.getenv("EDINET_API_KEY")


def _normalize_sec_code(ticker: str) -> str:
    """ユーザー入力の証券コードを EDINET secCode 比較用の 4 桁文字列に正規化する。

    EDINET の secCode は通常 5 桁（4桁コード + チェック数字1桁、多くは末尾0）。
    比較は先頭4桁で行う方が安全。
    """
    if not ticker:
        return ""
    s = "".join(c for c in str(ticker).strip() if c.isdigit())
    return s[:4]


async def list_documents_for_date(
    target_date: datetime.date,
    session: aiohttp.ClientSession,
    api_key: Optional[str] = None,
) -> list[dict]:
    """指定日に EDINET に提出された書類一覧を取得する。"""
    api_key = api_key or get_api_key()
    if not api_key:
        raise RuntimeError("EDINET_API_KEY が設定されていません")
    params = {
        "date": target_date.strftime("%Y-%m-%d"),
        "type": "2",
        "Subscription-Key": api_key,
    }
    url = f"{EDINET_API_BASE}/documents.json"
    async with session.get(url, params=params, timeout=20) as resp:
        if resp.status != 200:
            text = await resp.text()
            logging.warning(
                f"EDINET list {target_date} HTTP {resp.status}: {text[:200]}"
            )
            return []
        data = await resp.json()
    return data.get("results", []) or []


async def find_documents_for_security_code(
    ticker: str,
    days: int = 400,
    only_earnings: bool = True,
    progress_cb=None,
) -> dict:
    """過去 `days` 日分の EDINET 書類一覧を走査し、指定証券コードの提出書類を返す。

    - 過剰なリクエストを防ぐため `days` は最大 1500 (≒4年) に丸める
    - 並列度を制御してレート制限を回避（同時 8 並列）
    - 結果は新しい順
    """
    api_key = get_api_key()
    if not api_key:
        return {"ok": False, "error": "EDINET_API_KEY 未設定"}

    sec4 = _normalize_sec_code(ticker)
    if not sec4 or len(sec4) != 4:
        return {"ok": False, "error": "4桁の証券コードを指定してください"}

    days = max(1, min(int(days or 400), 1500))
    today = datetime.datetime.now(JST).date()
    target_dates = [today - datetime.timedelta(days=i) for i in range(days)]

    semaphore = asyncio.Semaphore(8)
    matched: list[dict] = []
    completed = {"n": 0}
    total = len(target_dates)

    async with aiohttp.ClientSession() as session:
        async def fetch_one(d: datetime.date):
            async with semaphore:
                try:
                    results = await list_documents_for_date(d, session, api_key)
                except Exception as e:
                    logging.debug(f"EDINET fetch {d} 失敗: {e}")
                    results = []
            completed["n"] += 1
            if progress_cb:
                try:
                    progress_cb(completed["n"], total)
                except Exception:
                    pass
            for r in results:
                sc = (r.get("secCode") or "")
                if sc[:4] == sec4:
                    if only_earnings and (r.get("docTypeCode") not in EARNINGS_DOC_TYPES):
                        continue
                    if not r.get("pdfFlag"):  # PDF が無い書類はスキップ
                        continue
                    matched.append(r)

        await asyncio.gather(*(fetch_one(d) for d in target_dates))

    # 提出日新しい順
    matched.sort(key=lambda r: r.get("submitDateTime") or "", reverse=True)

    docs = [
        {
            "doc_id": r.get("docID"),
            "sec_code": r.get("secCode"),
            "filer_name": r.get("filerName"),
            "doc_type_code": r.get("docTypeCode"),
            "doc_type_label": DOC_TYPE_LABELS.get(r.get("docTypeCode", ""), ""),
            "doc_description": r.get("docDescription"),
            "submit_datetime": r.get("submitDateTime"),
            "period_start": r.get("periodStart"),
            "period_end": r.get("periodEnd"),
            "pdf_flag": r.get("pdfFlag"),
            "xbrl_flag": r.get("xbrlFlag"),
        }
        for r in matched
    ]
    return {"ok": True, "ticker": sec4, "days_scanned": days, "documents": docs}


async def download_document(doc_id: str, doc_type: int = 2) -> Optional[bytes]:
    """EDINET から書類本体（type=2: PDF）をダウンロードしてバイト列を返す。失敗時 None。"""
    api_key = get_api_key()
    if not api_key:
        return None
    if not doc_id:
        return None
    url = f"{EDINET_API_BASE}/documents/{doc_id}"
    params = {"type": str(int(doc_type)), "Subscription-Key": api_key}
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                text = await resp.text()
                logging.warning(
                    f"EDINET download {doc_id} HTTP {resp.status}: {text[:200]}"
                )
                return None
            return await resp.read()
