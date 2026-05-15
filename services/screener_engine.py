"""日本株スクリーニングエンジン。

機械的に計算可能なテクニカル指標とスタイル別戦略を提供する。
Gemini を呼ばず Python のみで完結し、ハルシネーションの余地がない。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


# --- テクニカル指標群 (pandas/numpy のみで計算) ---


class TechnicalSignals:
    """純粋関数群。すべて pandas.Series を受け取り pandas.Series または float を返す。"""

    @staticmethod
    def sma(series, n: int):
        return series.rolling(window=n, min_periods=max(1, n // 2)).mean()

    @staticmethod
    def ema(series, n: int):
        return series.ewm(span=n, adjust=False, min_periods=max(1, n // 2)).mean()

    @staticmethod
    def bollinger(close, n: int = 20, k: float = 2.0):
        sma = close.rolling(window=n, min_periods=max(1, n // 2)).mean()
        std = close.rolling(window=n, min_periods=max(1, n // 2)).std(ddof=0)
        upper = sma + k * std
        lower = sma - k * std
        width = (upper - lower) / sma.replace(0, float("nan"))
        return sma, upper, lower, width

    @staticmethod
    def atr(high, low, close, n: int = 14):
        import pandas as pd  # type: ignore
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        return tr.rolling(window=n, min_periods=max(1, n // 2)).mean()

    @staticmethod
    def rsi(close, n: int = 14):
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
        avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
        rs = avg_gain / avg_loss.replace(0, float("nan"))
        return 100 - (100 / (1 + rs))

    @staticmethod
    def near_52w_high(close, high, tolerance: float = 0.01) -> tuple[bool, float, float]:
        """直近終値が 52 週(≈252日) 高値からどれだけ離れているか。

        Returns:
            (is_near, gap_ratio, high_52w)
            gap_ratio = (high_52w - close) / high_52w   ※ 0 に近いほど高値圏
        """
        if len(high) < 60:
            return False, 1.0, float("nan")
        window = high.tail(252) if len(high) >= 252 else high
        high_52w = float(window.max())
        if high_52w <= 0:
            return False, 1.0, high_52w
        latest_close = float(close.iloc[-1])
        gap = (high_52w - latest_close) / high_52w
        return gap <= tolerance, gap, high_52w

    @staticmethod
    def consecutive_up_count(close, threshold_low: float = 0.005, threshold_high: float = 0.03, lookback: int = 10) -> int:
        """直近 lookback 日のうち、+threshold_low〜+threshold_high の上昇日数をカウント。"""
        ret = close.pct_change().tail(lookback)
        mask = (ret >= threshold_low) & (ret <= threshold_high)
        return int(mask.sum())

    @staticmethod
    def volume_surge_ratio(volume, short: int = 5, long: int = 20) -> float:
        if len(volume) < long:
            return 1.0
        s = float(volume.tail(short).mean())
        l = float(volume.tail(long).mean())
        if l <= 0:
            return 1.0
        return s / l

    @staticmethod
    def latest_volume_vs_avg(volume, n: int = 20) -> float:
        if len(volume) < n:
            return 1.0
        avg = float(volume.tail(n).mean())
        if avg <= 0:
            return 1.0
        return float(volume.iloc[-1]) / avg

    @staticmethod
    def return_pct(close, n: int) -> float:
        if len(close) <= n:
            return 0.0
        old = float(close.iloc[-n - 1])
        new = float(close.iloc[-1])
        if old <= 0:
            return 0.0
        return (new - old) / old


# --- スクリーニング結果データクラス ---


@dataclass
class Signal:
    name: str
    value: str
    threshold: str
    passed: bool
    source: str = "yfinance OHLCV"


@dataclass
class ScreeningResult:
    code: str
    name: str
    sector: str
    style: str
    score: float
    signals: list[Signal] = field(default_factory=list)
    price_snapshot: dict = field(default_factory=dict)
    data_as_of: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "sector": self.sector,
            "style": self.style,
            "score": round(self.score, 2),
            "signals": [s.__dict__ for s in self.signals],
            "price_snapshot": self.price_snapshot,
            "data_as_of": self.data_as_of,
        }


# --- スタイル戦略 ---


class StyleStrategy(ABC):
    style_name: str = ""
    display_name: str = ""
    description: str = ""

    @abstractmethod
    def evaluate(self, code: str, name: str, sector: str, df, fundamentals: Optional[dict] = None) -> Optional[ScreeningResult]:
        """1 銘柄を評価してスコアリング。フィルタ通過しなければ None。"""

    @staticmethod
    def _build_snapshot(df) -> dict:
        try:
            close = float(df["Close"].iloc[-1])
            prev_close = float(df["Close"].iloc[-2]) if len(df) >= 2 else close
            change_pct = ((close - prev_close) / prev_close * 100) if prev_close > 0 else 0.0
            window = df["High"].tail(252) if len(df) >= 252 else df["High"]
            high_52w = float(window.max())
            low_window = df["Low"].tail(252) if len(df) >= 252 else df["Low"]
            low_52w = float(low_window.min())
            volume = int(df["Volume"].iloc[-1]) if df["Volume"].iloc[-1] == df["Volume"].iloc[-1] else 0
            return {
                "close": round(close, 2),
                "change_pct": round(change_pct, 2),
                "high_52w": round(high_52w, 2),
                "low_52w": round(low_52w, 2),
                "volume": volume,
            }
        except Exception:
            return {}

    @staticmethod
    def _data_as_of(df) -> str:
        try:
            ts = df.index[-1]
            return ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
        except Exception:
            return ""


class TrendFollowStrategy(StyleStrategy):
    style_name = "trend_follow"
    display_name = "順張り（52週高値ブレイク）"
    description = "52週高値付近かつ出来高が増加し、200日線より上にある銘柄"

    def evaluate(self, code, name, sector, df, fundamentals=None):
        if df is None or len(df) < 60:
            return None
        close = df["Close"]
        high = df["High"]
        volume = df["Volume"]

        is_near, gap, high_52w = TechnicalSignals.near_52w_high(close, high, tolerance=0.01)
        sma200 = TechnicalSignals.sma(close, 200)
        sma25 = TechnicalSignals.sma(close, 25)
        vol_ratio = TechnicalSignals.latest_volume_vs_avg(volume, n=20)
        latest_close = float(close.iloc[-1])
        sma200_val = float(sma200.iloc[-1]) if sma200.iloc[-1] == sma200.iloc[-1] else None
        sma25_val = float(sma25.iloc[-1]) if sma25.iloc[-1] == sma25.iloc[-1] else None

        sigs = [
            Signal(
                name="52週高値乖離",
                value=f"{gap * 100:.2f}%",
                threshold="≤1.00%",
                passed=is_near,
            ),
            Signal(
                name="出来高サージ (vs 20日平均)",
                value=f"{vol_ratio:.2f}x",
                threshold="≥1.50x",
                passed=vol_ratio >= 1.5,
            ),
            Signal(
                name="25日MA上抜け",
                value=f"{((latest_close - sma25_val) / sma25_val * 100):+.2f}%" if sma25_val else "N/A",
                threshold=">0%",
                passed=bool(sma25_val and latest_close > sma25_val),
            ),
            Signal(
                name="200日MA上抜け",
                value=f"{((latest_close - sma200_val) / sma200_val * 100):+.2f}%" if sma200_val else "N/A",
                threshold=">0%",
                passed=bool(sma200_val and latest_close > sma200_val),
            ),
        ]

        # 必須: 52週高値近傍 + 200日MA上 + 出来高1.5x
        must_pass = sigs[0].passed and sigs[3].passed and sigs[1].passed
        if not must_pass:
            return None

        passed_count = sum(1 for s in sigs if s.passed)
        score = passed_count / len(sigs) * 100
        # 高値近接ほど加点
        score += max(0, (0.01 - gap) * 1000)
        score = min(100.0, score)

        return ScreeningResult(
            code=code, name=name, sector=sector, style=self.style_name,
            score=score, signals=sigs,
            price_snapshot=self._build_snapshot(df),
            data_as_of=self._data_as_of(df),
        )


class CreepingBreakoutStrategy(StyleStrategy):
    style_name = "creeping_breakout"
    display_name = "じわじわ高値ブレイク（低ボラ）"
    description = "52週高値圏でじわじわ上昇し、急騰や下抜けがない、ブレイク前夜の銘柄"

    def evaluate(self, code, name, sector, df, fundamentals=None):
        if df is None or len(df) < 60:
            return None
        close = df["Close"]
        high = df["High"]
        low = df["Low"]

        is_near, gap, _high_52w = TechnicalSignals.near_52w_high(close, high, tolerance=0.05)

        sma200 = TechnicalSignals.sma(close, 200)
        sma200_val = float(sma200.iloc[-1]) if sma200.iloc[-1] == sma200.iloc[-1] else None
        latest_close = float(close.iloc[-1])
        above_sma200 = bool(sma200_val and latest_close > sma200_val)

        atr = TechnicalSignals.atr(high, low, close, n=14)
        atr_val = float(atr.iloc[-1]) if atr.iloc[-1] == atr.iloc[-1] else None
        atr_pct = (atr_val / latest_close) if (atr_val and latest_close > 0) else None
        low_vol = bool(atr_pct is not None and atr_pct < 0.03)

        recent_returns = close.pct_change().tail(10).dropna()
        max_daily_ret = float(recent_returns.max()) if not recent_returns.empty else 0.0
        no_big_pop = max_daily_ret <= 0.05

        recent_low = low.tail(6)
        if len(recent_low) >= 6:
            prev_lows = recent_low.shift(1).iloc[1:]
            curr_lows = recent_low.iloc[1:]
            breaks = int((curr_lows < prev_lows).sum())
        else:
            breaks = 99
        no_prev_low_break = breaks == 0

        sigs = [
            Signal(
                name="52週高値乖離",
                value=f"{gap * 100:.2f}%",
                threshold="≤5.00%",
                passed=is_near,
            ),
            Signal(
                name="200日MA上抜け",
                value=f"{((latest_close - sma200_val) / sma200_val * 100):+.2f}%" if sma200_val else "N/A",
                threshold=">0%",
                passed=above_sma200,
            ),
            Signal(
                name="ATR/Close (低ボラ)",
                value=f"{atr_pct * 100:.2f}%" if atr_pct else "N/A",
                threshold="<3.00%",
                passed=low_vol,
            ),
            Signal(
                name="直近10日 最大日次リターン",
                value=f"{max_daily_ret * 100:+.2f}%",
                threshold="≤+5.00%（大陽線なし）",
                passed=no_big_pop,
            ),
            Signal(
                name="直近5日 前日安値割れ回数",
                value=(f"{breaks}回" if breaks < 99 else "N/A"),
                threshold="0回",
                passed=no_prev_low_break,
            ),
        ]

        if not all(s.passed for s in sigs):
            return None

        passed_count = sum(1 for s in sigs if s.passed)
        score = passed_count / len(sigs) * 100
        score += max(0, (0.05 - gap) * 200)
        if atr_pct is not None:
            score += max(0, (0.03 - atr_pct) * 500)
        score = min(100.0, score)

        return ScreeningResult(
            code=code, name=name, sector=sector, style=self.style_name,
            score=score, signals=sigs,
            price_snapshot=self._build_snapshot(df),
            data_as_of=self._data_as_of(df),
        )


class CreepingUpStrategy(StyleStrategy):
    style_name = "creeping_up"
    display_name = "じわじわ上昇（注目集まり前）"
    description = "数%の上昇を連日続け、出来高もじわじわ増えており過熱はしていない銘柄"

    def evaluate(self, code, name, sector, df, fundamentals=None):
        if df is None or len(df) < 30:
            return None
        close = df["Close"]
        volume = df["Volume"]
        high = df["High"]
        low = df["Low"]

        up_days = TechnicalSignals.consecutive_up_count(
            close, threshold_low=0.005, threshold_high=0.03, lookback=10
        )
        vol_ratio = TechnicalSignals.volume_surge_ratio(volume, short=5, long=20)
        atr = TechnicalSignals.atr(high, low, close, n=14)
        atr_val = float(atr.iloc[-1]) if atr.iloc[-1] == atr.iloc[-1] else None
        latest_close = float(close.iloc[-1])
        atr_pct = (atr_val / latest_close) if (atr_val and latest_close > 0) else None
        ret_5d = TechnicalSignals.return_pct(close, 5)

        sigs = [
            Signal(
                name="連続上昇日数 (+0.5%〜+3%, 直近10日)",
                value=f"{up_days}日",
                threshold="≥3日",
                passed=up_days >= 3,
            ),
            Signal(
                name="出来高比率 (5日 / 20日)",
                value=f"{vol_ratio:.2f}x",
                threshold="≥1.10x",
                passed=vol_ratio >= 1.10,
            ),
            Signal(
                name="ATR/Close (過熱度)",
                value=f"{atr_pct * 100:.2f}%" if atr_pct else "N/A",
                threshold="<4.00%",
                passed=bool(atr_pct and atr_pct < 0.04),
            ),
            Signal(
                name="直近5日リターン",
                value=f"{ret_5d * 100:+.2f}%",
                threshold="0%〜+15%",
                passed=0 <= ret_5d <= 0.15,
            ),
        ]

        must_pass = sigs[0].passed and sigs[1].passed and sigs[2].passed
        if not must_pass:
            return None

        passed_count = sum(1 for s in sigs if s.passed)
        score = passed_count / len(sigs) * 100
        score += min(15.0, up_days * 1.5)
        score = min(100.0, score)

        return ScreeningResult(
            code=code, name=name, sector=sector, style=self.style_name,
            score=score, signals=sigs,
            price_snapshot=self._build_snapshot(df),
            data_as_of=self._data_as_of(df),
        )


class LowVolBreakoutStrategy(StyleStrategy):
    style_name = "low_vol_breakout"
    display_name = "待てば上がる（低ボラ収束）"
    description = "ボリンジャー収束で動きが少ない、ブレイク待ちの銘柄"

    def evaluate(self, code, name, sector, df, fundamentals=None):
        if df is None or len(df) < 100:
            return None
        close = df["Close"]
        _, _, _, width = TechnicalSignals.bollinger(close, n=20, k=2.0)
        # 直近 100 日でのバンド幅順位（小さいほど収束）
        recent_widths = width.tail(100).dropna()
        if len(recent_widths) < 20:
            return None
        latest_width = float(recent_widths.iloc[-1])
        rank_pct = float((recent_widths < latest_width).sum()) / len(recent_widths)
        # rank_pct が小さいほど(下位)良い → 「下位10%」= rank_pct <= 0.10
        ret_60 = TechnicalSignals.return_pct(close, 60) if len(close) > 60 else 0.0

        sigs = [
            Signal(
                name="BB幅 直近100日順位",
                value=f"下位 {rank_pct * 100:.1f}%",
                threshold="下位 ≤10%",
                passed=rank_pct <= 0.10,
            ),
            Signal(
                name="60日リターン絶対値",
                value=f"{abs(ret_60) * 100:.2f}%",
                threshold="≤15%",
                passed=abs(ret_60) <= 0.15,
            ),
        ]

        must_pass = sigs[0].passed and sigs[1].passed
        if not must_pass:
            return None

        # スコアは収束度（rank_pct が小さいほど高スコア）
        score = (1 - rank_pct) * 100
        score = min(100.0, max(0.0, score))

        return ScreeningResult(
            code=code, name=name, sector=sector, style=self.style_name,
            score=score, signals=sigs,
            price_snapshot=self._build_snapshot(df),
            data_as_of=self._data_as_of(df),
        )


class ValueStrategy(StyleStrategy):
    style_name = "value"
    display_name = "バリュー（割安+配当）"
    description = "PER/PBR が低く、財務健全で配当も出ている銘柄"

    def evaluate(self, code, name, sector, df, fundamentals=None):
        if not fundamentals:
            return None
        per = fundamentals.get("per")
        pbr = fundamentals.get("pbr")
        div_yield = fundamentals.get("dividend_yield")
        # yfinance の dividendYield は 0-1 の少数で返るケースと % 値のケースが混在するので両対応
        if div_yield is not None and div_yield > 1:
            div_yield = div_yield / 100.0

        sigs = [
            Signal(
                name="PER",
                value=f"{per:.2f}" if per else "N/A",
                threshold="<15",
                passed=bool(per and 0 < per < 15),
                source="yfinance fundamentals",
            ),
            Signal(
                name="PBR",
                value=f"{pbr:.2f}" if pbr else "N/A",
                threshold="<1.5",
                passed=bool(pbr and 0 < pbr < 1.5),
                source="yfinance fundamentals",
            ),
            Signal(
                name="配当利回り",
                value=f"{div_yield * 100:.2f}%" if div_yield is not None else "N/A",
                threshold="≥3%",
                passed=bool(div_yield is not None and div_yield >= 0.03),
                source="yfinance fundamentals",
            ),
        ]

        if not (sigs[0].passed and sigs[1].passed and sigs[2].passed):
            return None

        passed_count = sum(1 for s in sigs if s.passed)
        score = passed_count / len(sigs) * 100

        return ScreeningResult(
            code=code, name=name, sector=sector, style=self.style_name,
            score=score, signals=sigs,
            price_snapshot=self._build_snapshot(df) if df is not None else {},
            data_as_of=self._data_as_of(df) if df is not None else "",
        )


class GrowthStrategy(StyleStrategy):
    style_name = "growth"
    display_name = "グロース（成長性重視）"
    description = "売上・利益成長率と ROE が高い銘柄"

    def evaluate(self, code, name, sector, df, fundamentals=None):
        if not fundamentals:
            return None
        rev_growth = fundamentals.get("revenue_growth")
        earn_growth = fundamentals.get("earnings_growth")
        roe = fundamentals.get("roe")

        sigs = [
            Signal(
                name="売上成長率（直近）",
                value=f"{rev_growth * 100:.2f}%" if rev_growth is not None else "N/A",
                threshold="≥15%",
                passed=bool(rev_growth is not None and rev_growth >= 0.15),
                source="yfinance fundamentals",
            ),
            Signal(
                name="利益成長率（直近）",
                value=f"{earn_growth * 100:.2f}%" if earn_growth is not None else "N/A",
                threshold="≥15%",
                passed=bool(earn_growth is not None and earn_growth >= 0.15),
                source="yfinance fundamentals",
            ),
            Signal(
                name="ROE",
                value=f"{roe * 100:.2f}%" if roe is not None else "N/A",
                threshold="≥12%",
                passed=bool(roe is not None and roe >= 0.12),
                source="yfinance fundamentals",
            ),
        ]

        if sum(1 for s in sigs if s.passed) < 2:
            return None

        passed_count = sum(1 for s in sigs if s.passed)
        score = passed_count / len(sigs) * 100

        return ScreeningResult(
            code=code, name=name, sector=sector, style=self.style_name,
            score=score, signals=sigs,
            price_snapshot=self._build_snapshot(df) if df is not None else {},
            data_as_of=self._data_as_of(df) if df is not None else "",
        )


STRATEGY_REGISTRY: dict[str, StyleStrategy] = {
    s.style_name: s for s in [
        TrendFollowStrategy(),
        CreepingBreakoutStrategy(),
        CreepingUpStrategy(),
        LowVolBreakoutStrategy(),
        ValueStrategy(),
        GrowthStrategy(),
    ]
}


def get_strategy(name: str) -> Optional[StyleStrategy]:
    return STRATEGY_REGISTRY.get(name)


def list_strategies() -> list[dict]:
    return [
        {
            "name": s.style_name,
            "display_name": s.display_name,
            "description": s.description,
            "needs_fundamentals": s.style_name in ("value", "growth"),
        }
        for s in STRATEGY_REGISTRY.values()
    ]
