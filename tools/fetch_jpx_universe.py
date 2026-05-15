"""JPX 公式の上場銘柄一覧 (.xls) をダウンロードして data/jp_universe_all.csv を生成する。

事前準備（必要なパッケージのインストール）:
    pip install requests xlrd==1.2.0

実行:
    python tools/fetch_jpx_universe.py

生成後、PWA のスクリーナーでユニバース「all」を選択できるようになる。

注意:
- JPX の XLS ファイル URL はリンク切れになることがあるので、その場合は
  https://www.jpx.co.jp/markets/statistics-equities/misc/01.html
  から最新の URL を取得して、JPX_URL を書き換えること。
- ETF/REIT は除外する（証券コードが4桁数字でないものをスキップ）。
"""
from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

JPX_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/misc/"
    "tvdivq0000001vg2-att/data_j.xls"
)
OUT_CSV = Path(__file__).parent.parent / "data" / "jp_universe_all.csv"


def main() -> int:
    try:
        import requests  # type: ignore
        import xlrd  # type: ignore
    except ImportError as e:
        print(f"[エラー] 必要なパッケージが未インストール: {e}", file=sys.stderr)
        print("        pip install requests xlrd==1.2.0", file=sys.stderr)
        return 1

    print(f"[1/3] JPX からダウンロード中: {JPX_URL}")
    resp = requests.get(JPX_URL, timeout=60)
    resp.raise_for_status()
    print(f"      {len(resp.content):,} bytes")

    print("[2/3] Excel をパース中...")
    book = xlrd.open_workbook(file_contents=resp.content)
    sheet = book.sheet_by_index(0)

    headers = [str(sheet.cell_value(0, c)).strip() for c in range(sheet.ncols)]

    def col_idx(*candidates: str) -> int:
        for c in candidates:
            for i, h in enumerate(headers):
                if c in h:
                    return i
        return -1

    code_idx = col_idx("コード", "Code")
    name_idx = col_idx("銘柄名", "Name")
    market_idx = col_idx("市場", "Market")
    sector_idx = col_idx("33業種区分", "33業種", "Sector")

    if code_idx < 0 or name_idx < 0:
        print(f"[エラー] 必要な列が見つかりません: headers={headers}", file=sys.stderr)
        return 2

    items: list[dict] = []
    for r in range(1, sheet.nrows):
        code_raw = sheet.cell_value(r, code_idx)
        if isinstance(code_raw, float):
            code = str(int(code_raw))
        else:
            code = str(code_raw).strip()
        if not (code.isdigit() and len(code) == 4):
            continue
        market = str(sheet.cell_value(r, market_idx)).strip() if market_idx >= 0 else ""
        if market and not any(k in market for k in ("プライム", "スタンダード", "グロース")):
            continue
        name = str(sheet.cell_value(r, name_idx)).strip()
        sector = str(sheet.cell_value(r, sector_idx)).strip() if sector_idx >= 0 else ""
        items.append({"code": code, "name": name, "sector": sector})

    print(f"      {len(items)} 銘柄を抽出")

    print(f"[3/3] CSV に書き出し: {OUT_CSV}")
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["code", "name", "sector"])
        writer.writeheader()
        writer.writerows(items)

    print("完了しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
