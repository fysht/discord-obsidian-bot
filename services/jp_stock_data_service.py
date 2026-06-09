"""日本株データ取得サービス。

yfinance を MVP プロバイダとして実装。J-Quants は Phase 2 で追加可能なよう抽象化している。

OHLCV は SQLite (`stock_ohlcv` テーブル) にキャッシュし、当日分のみ差分取得する。
"""
from __future__ import annotations

import asyncio
import csv
import datetime
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from config import JST


_PROVIDER_SINGLETON: Optional["StockDataProvider"] = None
_DATA_DIR = Path(__file__).parent.parent / "data"

# S&P500 構成銘柄の取得元（安定した CSV。シンボルの "." は yfinance 互換のため "-" に変換）
_US_SP500_URLS = [
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv",
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv",
]


async def _fetch_us_sp500_to_csv(path: Path) -> bool:
    """S&P500 構成銘柄を取得して us_universe_sp500.csv を書き出す（実行時の自己修復用）。"""
    import io

    import aiohttp

    headers = {"User-Agent": "Mozilla/5.0"}
    timeout = aiohttp.ClientTimeout(total=30)
    for url in _US_SP500_URLS:
        try:
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    text = await resp.text()
        except Exception as e:
            logging.debug(f"S&P500 取得失敗 {url}: {e}")
            continue
        rows = []
        for row in csv.DictReader(io.StringIO(text)):
            sym = (row.get("Symbol") or row.get("symbol") or "").strip().upper()
            if not sym:
                continue
            sym = sym.replace(".", "-")  # BRK.B → BRK-B
            rows.append({
                "code": sym,
                "name": (row.get("Security") or row.get("Name") or "").strip(),
                "sector": (row.get("GICS Sector") or row.get("Sector") or "").strip(),
            })
        if rows:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("w", encoding="utf-8", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=["code", "name", "sector"])
                    writer.writeheader()
                    writer.writerows(rows)
                logging.info(f"S&P500 ユニバースを自動取得しました（{len(rows)}銘柄）")
                return True
            except Exception as e:
                logging.warning(f"S&P500 CSV 書き出し失敗: {e}")
                return False
    return False


class StockDataProvider(ABC):
    """株価データ取得の抽象基底クラス。"""

    @abstractmethod
    async def fetch_ohlcv_remote(self, code: str, days: int = 300):
        """リモートから OHLCV を取得する（実装側はキャッシュを使わない）。

        Returns:
            pandas.DataFrame: index=Date, columns=[Open, High, Low, Close, Volume]
        """

    @abstractmethod
    async def get_fundamentals(self, code: str) -> dict:
        """ファンダメンタル指標を取得する。

        Returns:
            dict: {market_cap_jpy, per, pbr, roe, dividend_yield, equity_ratio, ...}
            取得できない値は None として返す。
        """

    async def get_ohlcv(self, code: str, days: int = 300, force_refresh: bool = False):
        """SQLite キャッシュ経由で OHLCV を取得する。

        - キャッシュに最新日まであればそれを返す
        - 不足分のみリモートから取得して upsert
        - 戻り値は pandas.DataFrame (index=DatetimeIndex, columns=Open/High/Low/Close/Volume)
        """
        try:
            import pandas as pd  # type: ignore
        except ImportError:
            logging.error("pandas 未インストール。requirements.txt を確認してください")
            return None

        from api.database import get_ohlcv_latest_date, get_ohlcv_range, upsert_ohlcv_rows

        now_jst = datetime.datetime.now(JST)
        today = now_jst.date()
        start_date = (today - datetime.timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")

        # 東証クローズ(15:00 JST)後は当日終値を必須にする。クローズ前は前営業日まで OK。
        # 週末・祝日は前営業日(=金曜)分が揃っていれば OK。
        def _last_expected_close_date(now: datetime.datetime) -> datetime.date:
            """現時刻時点で、キャッシュに含まれているべき最新営業日を返す。"""
            d = now.date()
            # 平日 15:00 以降 → 今日の引け値が確定済み
            after_close = now.weekday() < 5 and (now.hour, now.minute) >= (15, 0)
            if not after_close:
                # 当日終値がまだなので、最新は前営業日
                d = d - datetime.timedelta(days=1)
            # 週末を遡って直近の平日へ
            while d.weekday() >= 5:
                d = d - datetime.timedelta(days=1)
            return d

        expected_latest = _last_expected_close_date(now_jst)

        latest = None if force_refresh else await get_ohlcv_latest_date(code)
        need_remote = True
        if latest:
            try:
                latest_date = datetime.date.fromisoformat(latest)
                # キャッシュが想定する最新営業日まで揃っていればリモート不要
                if latest_date >= expected_latest:
                    need_remote = False
            except ValueError:
                pass

        if need_remote:
            df_remote = await self.fetch_ohlcv_remote(code, days=days)
            if df_remote is not None and len(df_remote) > 0:
                rows = []
                for ts, row in df_remote.iterrows():
                    try:
                        date_str = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
                        rows.append({
                            "date": date_str,
                            "open": _safe_float(row.get("Open")),
                            "high": _safe_float(row.get("High")),
                            "low": _safe_float(row.get("Low")),
                            "close": _safe_float(row.get("Close")),
                            "volume": _safe_int(row.get("Volume")),
                        })
                    except Exception:
                        continue
                if rows:
                    await upsert_ohlcv_rows(code, rows)

        cached = await get_ohlcv_range(code, start_date=start_date)
        if not cached:
            return None
        df = pd.DataFrame(cached)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        df = df.rename(columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume",
        })
        return df.tail(days)

    async def get_universe(self, name: str = "topix500") -> list[dict]:
        """ユニバース（銘柄一覧）を CSV から読み込む。

        Args:
            name: "topix500" / "all" など

        Returns:
            [{"code": "7203", "name": "トヨタ自動車", "sector": "輸送用機器"}, ...]
        """
        # 米国株ユニバースは "us_" プレフィックスで区別（例: us_sp500 → us_universe_sp500.csv）。
        if name.startswith("us_"):
            path = _DATA_DIR / f"us_universe_{name[3:]}.csv"
        else:
            path = _DATA_DIR / f"jp_universe_{name}.csv"
            if not path.exists():
                # フォールバック: us_universe_{name}.csv も探す
                alt = _DATA_DIR / f"us_universe_{name}.csv"
                if alt.exists():
                    path = alt
        if not path.exists():
            # S&P500 は CSV が無ければ実行時に自動取得（スクリプト実行不要・自己修復）
            if name == "us_sp500":
                if not await _fetch_us_sp500_to_csv(path):
                    logging.warning("S&P500 ユニバースの自動取得に失敗しました")
                    return []
            else:
                logging.warning(f"ユニバースCSVが見つかりません: {path}")
                return []
        result: list[dict] = []
        with path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                code = (row.get("code") or "").strip()
                if not code:
                    continue
                result.append({
                    "code": code,
                    "name": (row.get("name") or "").strip(),
                    "sector": (row.get("sector") or "").strip(),
                })
        return result

    async def list_universes(self) -> list[str]:
        names: list[str] = []
        if not _DATA_DIR.exists():
            return names
        for p in _DATA_DIR.glob("jp_universe_*.csv"):
            name = p.stem.replace("jp_universe_", "", 1)
            if name:
                names.append(name)
        # 米国株ユニバースは "us_" プレフィックス付きで列挙（例: us_sp500）
        for p in _DATA_DIR.glob("us_universe_*.csv"):
            name = p.stem.replace("us_universe_", "", 1)
            if name:
                names.append(f"us_{name}")
        # S&P500 は CSV が無くても選択肢として常に出す（選択時に自動取得）
        names.append("us_sp500")
        return sorted(set(names))


class YFinanceProvider(StockDataProvider):
    """yfinance ベースのプロバイダ。"""

    def __init__(self, max_concurrent: int = 5, sleep_per_call: float = 0.2):
        self._sem = asyncio.Semaphore(max_concurrent)
        self._sleep = sleep_per_call

    @staticmethod
    def _to_yf_ticker(code: str) -> str:
        c = (code or "").strip().upper()
        if "." in c:
            return c
        # 4桁数字なら東証扱い
        if c.isdigit() and len(c) == 4:
            return f"{c}.T"
        return c

    async def fetch_ohlcv_remote(self, code: str, days: int = 300):
        try:
            import yfinance as yf  # type: ignore
        except ImportError:
            logging.error("yfinance 未インストール。requirements.txt を確認してください")
            return None
        ticker = self._to_yf_ticker(code)
        period = f"{max(days, 60)}d" if days <= 730 else "max"

        async with self._sem:
            await asyncio.sleep(self._sleep)

            def _fetch():
                try:
                    t = yf.Ticker(ticker)
                    # auto_adjust=True で分割・配当を遡及調整した OHLC を取得する。
                    # こうしないと過去データが歪み、チャート（調整済み表示）と
                    # スクリーニング判定（無調整）が食い違う原因になる。
                    df = t.history(period=period, auto_adjust=True)
                    return df
                except Exception as e:
                    logging.debug(f"yfinance fetch_ohlcv_remote error for {ticker}: {e}")
                    return None

            df = await asyncio.to_thread(_fetch)
            if df is None or len(df) == 0:
                return None
            keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
            df = df[keep].copy()
            return df

    async def get_fundamentals(self, code: str) -> dict:
        try:
            import yfinance as yf  # type: ignore
        except ImportError:
            return {}
        ticker = self._to_yf_ticker(code)

        async with self._sem:
            await asyncio.sleep(self._sleep)

            def _fetch():
                try:
                    t = yf.Ticker(ticker)
                    info = t.info or {}
                    return info
                except Exception as e:
                    logging.debug(f"yfinance get_fundamentals error for {ticker}: {e}")
                    return {}

            info = await asyncio.to_thread(_fetch)
            if not info:
                return {}

            return {
                "market_cap_jpy": info.get("marketCap"),
                "per": info.get("trailingPE"),
                "forward_per": info.get("forwardPE"),
                "pbr": info.get("priceToBook"),
                "peg": info.get("pegRatio"),
                "roe": info.get("returnOnEquity"),
                "operating_margin": info.get("operatingMargins"),
                "profit_margin": info.get("profitMargins"),
                "revenue": info.get("totalRevenue"),
                "shares_outstanding": info.get("sharesOutstanding"),
                "revenue_growth": info.get("revenueGrowth"),
                "earnings_growth": info.get("earningsGrowth"),
                "dividend_yield": info.get("dividendYield"),
                "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
                "currency": info.get("currency", "JPY"),
                "name": info.get("longName") or info.get("shortName"),
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "fetched_at": datetime.datetime.now(JST).isoformat(),
            }


class JQuantsProvider(StockDataProvider):
    """J-Quants API ベースのプロバイダ（Phase 2 で実装）。"""

    async def fetch_ohlcv_remote(self, code: str, days: int = 300):
        raise NotImplementedError("JQuantsProvider は Phase 2 で実装予定")

    async def get_fundamentals(self, code: str) -> dict:
        raise NotImplementedError("JQuantsProvider は Phase 2 で実装予定")


def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _safe_int(v) -> int | None:
    f = _safe_float(v)
    if f is None:
        return None
    try:
        return int(f)
    except (TypeError, ValueError):
        return None


def get_provider() -> StockDataProvider:
    """シングルトンプロバイダを取得する。

    環境変数 STOCK_DATA_PROVIDER で切替可能 (yfinance|jquants)。
    """
    global _PROVIDER_SINGLETON
    if _PROVIDER_SINGLETON is not None:
        return _PROVIDER_SINGLETON
    name = (os.getenv("STOCK_DATA_PROVIDER") or "yfinance").lower()
    if name == "jquants":
        _PROVIDER_SINGLETON = JQuantsProvider()
    else:
        _PROVIDER_SINGLETON = YFinanceProvider()
    return _PROVIDER_SINGLETON
