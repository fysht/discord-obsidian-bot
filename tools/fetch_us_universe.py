"""S&P 500 構成銘柄を取得して data/us_universe_sp500.csv を生成する。

事前準備:
    pip install requests

実行:
    python tools/fetch_us_universe.py

生成後、PWA のスクリーナーでユニバース「us_sp500」を選択できるようになる。
（即利用できる主要銘柄セット「us_mega」は data/us_universe_mega.csv に同梱済み。）

データ源（安定した CSV。リンク切れ時は SOURCES の別URLを使う）:
- datahub の S&P 500 constituents（Symbol, Security, GICS Sector）
yfinance 互換のため、シンボルの "." は "-" に変換する（例: BRK.B → BRK-B）。
"""
from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

import requests

SOURCES = [
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv",
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv",
]
OUT_CSV = Path(__file__).parent.parent / "data" / "us_universe_sp500.csv"


def _fetch() -> str:
    last_err = None
    for url in SOURCES:
        try:
            resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200 and resp.text:
                return resp.text
            last_err = f"HTTP {resp.status_code}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    raise RuntimeError(f"S&P500 一覧の取得に失敗しました: {last_err}")


def main() -> int:
    text = _fetch()
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        sym = (row.get("Symbol") or row.get("symbol") or "").strip().upper()
        name = (row.get("Security") or row.get("Name") or "").strip()
        sector = (row.get("GICS Sector") or row.get("Sector") or "").strip()
        if not sym:
            continue
        sym = sym.replace(".", "-")  # yfinance 互換（BRK.B → BRK-B）
        rows.append({"code": sym, "name": name, "sector": sector})

    if not rows:
        print("構成銘柄を1件も抽出できませんでした。SOURCES のURLを確認してください。", file=sys.stderr)
        return 1

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["code", "name", "sector"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"✅ {len(rows)} 銘柄を {OUT_CSV} に書き出しました（ユニバース名: us_sp500）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
