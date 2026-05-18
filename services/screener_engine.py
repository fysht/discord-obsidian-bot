"""日本株スクリーニングエンジン。

機械的に計算可能なテクニカル指標とスタイル別戦略を提供する。
Gemini を呼ばず Python のみで完結し、ハルシネーションの余地がない。

各戦略は「構成要素 (FilterDef)」を公開し、UI からチェックボックスで
ON/OFF できる。OFF にされた構成要素は「必須条件から外す」扱いになり、
評価のスコアリングからも除外される。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional


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

    @staticmethod
    def avg_wick_ratio(open_s, high, low, close, n: int = 10) -> tuple[float, float]:
        """直近 n 本の (上ひげ平均比, 下ひげ平均比) を全足レンジ比で返す。

        上ひげ比 = (high - max(open, close)) / (high - low)
        下ひげ比 = (min(open, close) - low)  / (high - low)
        """
        import pandas as pd  # type: ignore
        if len(close) < n or len(open_s) < n:
            return 0.0, 0.0
        o = open_s.tail(n).reset_index(drop=True)
        h = high.tail(n).reset_index(drop=True)
        l = low.tail(n).reset_index(drop=True)
        c = close.tail(n).reset_index(drop=True)
        rng = (h - l).replace(0, float("nan"))
        body_high = pd.concat([o, c], axis=1).max(axis=1)
        body_low = pd.concat([o, c], axis=1).min(axis=1)
        upper = ((h - body_high) / rng).dropna()
        lower = ((body_low - l) / rng).dropna()
        u = float(upper.mean()) if len(upper) else 0.0
        d = float(lower.mean()) if len(lower) else 0.0
        return u, d


# --- データクラス ---


@dataclass
class Signal:
    name: str
    value: str
    threshold: str
    passed: bool
    source: str = "yfinance OHLCV"


@dataclass
class FilterDef:
    """戦略を構成する「条件」のメタ情報。UIに公開してON/OFFを受け取る。"""
    key: str
    label: str
    description: str
    default: bool = True


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
    is_near_miss: bool = False
    failed_filters: list[str] = field(default_factory=list)

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
            "is_near_miss": self.is_near_miss,
            "failed_filters": self.failed_filters,
        }


# --- スタイル戦略 ---


class StyleStrategy(ABC):
    style_name: str = ""
    display_name: str = ""
    description: str = ""
    filters: list[FilterDef] = []

    @abstractmethod
    def evaluate(
        self,
        code: str,
        name: str,
        sector: str,
        df,
        fundamentals: Optional[dict] = None,
        enabled_filters: Optional[set[str]] = None,
        near_miss: bool = False,
    ) -> Optional[ScreeningResult]:
        """1 銘柄を評価してスコアリング。必須条件を満たさなければ None。

        Args:
            enabled_filters: ON にされている構成要素のキー集合。
                             None の場合は全構成要素 ON とみなす。
            near_miss: True の場合、必須条件を満たさなくても部分スコアで返す
                      （満たさなかった条件は failed_filters に記録）。
        """

    @classmethod
    def list_filters(cls) -> list[dict]:
        return [
            {"key": f.key, "label": f.label, "description": f.description, "default": f.default}
            for f in cls.filters
        ]

    @classmethod
    def _resolve_enabled(cls, enabled_filters: Optional[set[str]]) -> set[str]:
        if enabled_filters is None:
            return {f.key for f in cls.filters if f.default}
        return enabled_filters

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

    def _finalize(
        self,
        code: str,
        name: str,
        sector: str,
        sigs: list[Signal],
        enabled: set[str],
        signal_keys: list[str],
        df,
        score_bonus: float = 0.0,
        near_miss: bool = False,
    ) -> Optional[ScreeningResult]:
        """共通: ON のシグナルが全て passed なら ScreeningResult を返す。

        near_miss=True なら、満たさない条件があっても部分スコアで返す（失敗条件名を記録）。

        Args:
            signal_keys: sigs と同じ順序で各 Signal の filter_key を並べたもの
        """
        active_pairs = [(s, key) for s, key in zip(sigs, signal_keys) if key in enabled]
        if not active_pairs:
            return None
        active_sigs = [s for s, _ in active_pairs]
        all_passed = all(s.passed for s in active_sigs)
        if not all_passed and not near_miss:
            return None
        passed_count = sum(1 for s in active_sigs if s.passed)
        # near miss モードでは bonus を付けず、純粋な通過率でスコア化
        if all_passed:
            score = passed_count / len(active_sigs) * 100 + score_bonus
        else:
            score = passed_count / len(active_sigs) * 100
        score = min(100.0, max(0.0, score))
        failed = [s.name for s, _ in active_pairs if not s.passed]
        return ScreeningResult(
            code=code, name=name, sector=sector, style=self.style_name,
            score=score, signals=active_sigs,
            price_snapshot=self._build_snapshot(df),
            data_as_of=self._data_as_of(df),
            is_near_miss=not all_passed,
            failed_filters=failed,
        )


class CreepingBreakoutStrategy(StyleStrategy):
    style_name = "creeping_breakout"
    display_name = "じわじわ高値ブレイク（低ボラ）"
    description = "52週高値圏でじわじわ上昇し、急騰や下抜けがない、ブレイク前夜の銘柄"
    filters = [
        # 【位置】高値圏に位置している
        FilterDef("near_high", "52週高値乖離 ≤ 5%", "高値圏に位置している", True),
        FilterDef("above_sma200", "200日MA上抜け", "中長期の上昇トレンド", True),
        # 【トレンド】じわじわ上昇している
        FilterDef("up_days", "連続上昇日数 ≥3日（+0.5〜+3%）", "緩やかな連続上昇", True),
        FilterDef("ret_5d", "直近5日リターン 0〜+15%", "短期上昇しているが過熱なし", True),
        FilterDef("vol_increase", "出来高比率 5日/20日 ≥1.10x", "出来高がじわじわ増加", True),
        # 【品質】過熱なし・低ボラ
        FilterDef("low_volatility", "ATR/Close < 3%（低ボラ）", "値動きが激しすぎない", True),
        FilterDef("no_big_pop", "直近10日 大陽線（+5%超）なし", "大跳ねしていない", True),
        FilterDef("no_prev_low_break", "直近5日 前日安値割れなし", "押し目が浅い", True),
        # 【品質】ひげが長くない（騙し抑制）
        FilterDef("short_upper_wick", "上ひげ平均 ≤ 35%", "戻り売りに押されていない", True),
        FilterDef("short_lower_wick", "下ひげ平均 ≤ 35%", "押し戻しが軽い", True),
    ]

    def evaluate(self, code, name, sector, df, fundamentals=None, enabled_filters=None, near_miss=False):
        if df is None or len(df) < 60:
            return None
        enabled = self._resolve_enabled(enabled_filters)
        close = df["Close"]
        high = df["High"]
        low = df["Low"]
        volume = df["Volume"]

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

        up_days = TechnicalSignals.consecutive_up_count(close, 0.005, 0.03, 10)
        vol_ratio = TechnicalSignals.volume_surge_ratio(volume, 5, 20)
        ret_5d = TechnicalSignals.return_pct(close, 5)

        open_s = df["Open"] if "Open" in df.columns else close
        avg_upper_wick, avg_lower_wick = TechnicalSignals.avg_wick_ratio(open_s, high, low, close, n=10)
        short_upper = avg_upper_wick <= 0.35
        short_lower = avg_lower_wick <= 0.35

        sigs = [
            # 【位置】高値圏
            Signal("52週高値乖離 ≤ 5%", f"{gap * 100:.2f}%", "≤5.00%", is_near),
            Signal(
                "200日MA上抜け",
                f"{((latest_close - sma200_val) / sma200_val * 100):+.2f}%" if sma200_val else "N/A",
                ">0%",
                above_sma200,
            ),
            # 【トレンド】じわじわ上昇
            Signal("連続上昇日数 ≥3日（+0.5〜+3%）", f"{up_days}日", "≥3日", up_days >= 3),
            Signal("直近5日リターン 0〜+15%", f"{ret_5d * 100:+.2f}%", "0%〜+15%", 0 <= ret_5d <= 0.15),
            Signal("出来高比率 5日/20日 ≥1.10x", f"{vol_ratio:.2f}x", "≥1.10x", vol_ratio >= 1.10),
            # 【品質】過熱なし・低ボラ
            Signal("ATR/Close < 3%（低ボラ）", f"{atr_pct * 100:.2f}%" if atr_pct else "N/A", "<3.00%", low_vol),
            Signal("直近10日 大陽線（+5%超）なし", f"最大{max_daily_ret * 100:+.2f}%", "≤+5.00%", no_big_pop),
            Signal("直近5日 前日安値割れなし", f"{breaks}回" if breaks < 99 else "N/A", "0回", no_prev_low_break),
            Signal("上ひげ平均 ≤ 35%", f"{avg_upper_wick * 100:.1f}%", "≤35%", short_upper),
            Signal("下ひげ平均 ≤ 35%", f"{avg_lower_wick * 100:.1f}%", "≤35%", short_lower),
        ]
        signal_keys = [
            "near_high", "above_sma200",
            "up_days", "ret_5d", "vol_increase",
            "low_volatility", "no_big_pop", "no_prev_low_break",
            "short_upper_wick", "short_lower_wick",
        ]

        bonus = 0.0
        bonus += max(0, (0.05 - gap) * 200)
        if atr_pct is not None:
            bonus += max(0, (0.03 - atr_pct) * 500)

        return self._finalize(code, name, sector, sigs, enabled, signal_keys, df, score_bonus=bonus, near_miss=near_miss)


class ValueStrategy(StyleStrategy):
    style_name = "value"
    display_name = "バリュー（割安+配当）"
    description = "PER/PBR が低く、財務健全で配当も出ている銘柄"
    filters = [
        FilterDef("per", "PER < 15", "利益面で割安", True),
        FilterDef("pbr", "PBR < 1.5", "純資産面で割安", True),
        FilterDef("dividend", "配当利回り ≥ 3%", "インカム狙い", True),
    ]

    def evaluate(self, code, name, sector, df, fundamentals=None, enabled_filters=None, near_miss=False):
        if not fundamentals:
            return None
        enabled = self._resolve_enabled(enabled_filters)
        per = fundamentals.get("per")
        pbr = fundamentals.get("pbr")
        div_yield = fundamentals.get("dividend_yield")
        if div_yield is not None and div_yield > 1:
            div_yield = div_yield / 100.0

        sigs = [
            Signal("PER", f"{per:.2f}" if per else "N/A", "<15", bool(per and 0 < per < 15), "yfinance fundamentals"),
            Signal("PBR", f"{pbr:.2f}" if pbr else "N/A", "<1.5", bool(pbr and 0 < pbr < 1.5), "yfinance fundamentals"),
            Signal(
                "配当利回り",
                f"{div_yield * 100:.2f}%" if div_yield is not None else "N/A",
                "≥3%",
                bool(div_yield is not None and div_yield >= 0.03),
                "yfinance fundamentals",
            ),
        ]
        signal_keys = ["per", "pbr", "dividend"]
        return self._finalize(code, name, sector, sigs, enabled, signal_keys, df, near_miss=near_miss)


class GrowthStrategy(StyleStrategy):
    style_name = "growth"
    display_name = "グロース（成長性重視）"
    description = "売上・利益成長率と ROE が高い銘柄"
    filters = [
        FilterDef("revenue_growth", "売上成長率 ≥ 15%", "トップライン成長", True),
        FilterDef("earnings_growth", "利益成長率 ≥ 15%", "ボトムライン成長", True),
        FilterDef("roe", "ROE ≥ 12%", "資本効率が高い", True),
    ]

    def evaluate(self, code, name, sector, df, fundamentals=None, enabled_filters=None, near_miss=False):
        if not fundamentals:
            return None
        enabled = self._resolve_enabled(enabled_filters)
        rev_growth = fundamentals.get("revenue_growth")
        earn_growth = fundamentals.get("earnings_growth")
        roe = fundamentals.get("roe")

        sigs = [
            Signal(
                "売上成長率",
                f"{rev_growth * 100:.2f}%" if rev_growth is not None else "N/A",
                "≥15%",
                bool(rev_growth is not None and rev_growth >= 0.15),
                "yfinance fundamentals",
            ),
            Signal(
                "利益成長率",
                f"{earn_growth * 100:.2f}%" if earn_growth is not None else "N/A",
                "≥15%",
                bool(earn_growth is not None and earn_growth >= 0.15),
                "yfinance fundamentals",
            ),
            Signal(
                "ROE",
                f"{roe * 100:.2f}%" if roe is not None else "N/A",
                "≥12%",
                bool(roe is not None and roe >= 0.12),
                "yfinance fundamentals",
            ),
        ]
        signal_keys = ["revenue_growth", "earnings_growth", "roe"]
        return self._finalize(code, name, sector, sigs, enabled, signal_keys, df, near_miss=near_miss)


STRATEGY_REGISTRY: dict[str, StyleStrategy] = {
    s.style_name: s for s in [
        CreepingBreakoutStrategy(),
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
            "filters": s.list_filters(),
        }
        for s in STRATEGY_REGISTRY.values()
    ]
