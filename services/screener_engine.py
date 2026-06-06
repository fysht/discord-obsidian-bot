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
        # min_periods は n と同じにして、データ不足時は NaN を返す。
        # 緩めると新規上場銘柄で「200日MA」と称して 100日相当の値が出てしまい
        # チャート上のMAと食い違う原因になる。
        return series.rolling(window=n, min_periods=n).mean()

    @staticmethod
    def ema(series, n: int):
        return series.ewm(span=n, adjust=False, min_periods=n).mean()

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
    def consecutive_bullish_candles(open_s, close, lookback: int = 10) -> int:
        """最新の足から遡って連続している陽線（Close > Open）の本数を返す。"""
        if open_s is None or close is None:
            return 0
        n = min(len(open_s), len(close), lookback)
        if n <= 0:
            return 0
        cnt = 0
        for i in range(1, n + 1):
            o = float(open_s.iloc[-i])
            c = float(close.iloc[-i])
            if o != o or c != c:
                break
            if c > o:
                cnt += 1
            else:
                break
        return cnt

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
    # True の戦略は評価に fundamentals（get_fundamentals）の取得を要する
    needs_fundamentals: bool = False

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
        import math

        def _finite(v):
            """float へ変換し、NaN/Inf は None を返す（JSON 非互換値を排除）。"""
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f if math.isfinite(f) else None

        def _r(v):
            return round(v, 2) if v is not None else None

        try:
            close = _finite(df["Close"].iloc[-1])
            if close is None:
                # 上場廃止などで価格データが欠損している銘柄はスナップショット無し
                return {}
            prev_close = _finite(df["Close"].iloc[-2]) if len(df) >= 2 else close
            if prev_close is None:
                prev_close = close
            change_pct = ((close - prev_close) / prev_close * 100) if prev_close > 0 else 0.0
            window = df["High"].tail(252) if len(df) >= 252 else df["High"]
            high_52w = _finite(window.max())
            low_window = df["Low"].tail(252) if len(df) >= 252 else df["Low"]
            low_52w = _finite(low_window.min())
            vol_raw = df["Volume"].iloc[-1]
            volume = int(vol_raw) if vol_raw == vol_raw else 0
            return {
                "close": _r(close),
                "change_pct": _r(change_pct),
                "high_52w": _r(high_52w),
                "low_52w": _r(low_52w),
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
    description = "52週高値圏でじわじわ上昇し直近で高値を更新、急騰や下抜けがない銘柄"
    filters = [
        # 【位置】高値圏に位置している
        FilterDef("near_high", "52週高値乖離 ≤ 5%", "高値圏に位置している", True),
        FilterDef("new_high", "直近5日で52週高値を更新", "実際に高値を更新した", True),
        FilterDef("above_sma200", "200日MA上抜け", "中長期の上昇トレンド", True),
        # 【トレンド】じわじわ上昇している
        FilterDef("up_days", "連続上昇日数 ≥3日（+0.5〜+3%）", "緩やかな連続上昇", True),
        FilterDef("bullish_streak", "連続陽線 ≥3本", "直近で陽線が続いている", True),
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
        # 「200日MA上抜け」「52週高値」を扱うため、最低 200 営業日分は要求する。
        # 不足する銘柄は除外（短い履歴で誤判定するより安全）。
        if df is None or len(df) < 200:
            return None
        enabled = self._resolve_enabled(enabled_filters)
        close = df["Close"]
        high = df["High"]
        low = df["Low"]
        volume = df["Volume"]

        is_near, gap, _high_52w = TechnicalSignals.near_52w_high(close, high, tolerance=0.05)

        # 【位置】直近5営業日で実際に 52週高値を更新したか。
        # 直近5日の高値が、それ以前（最大52週）の高値以上なら「高値更新」とみなす。
        high_252 = high.tail(252)
        if len(high_252) >= 25:
            prior_max = float(high_252.iloc[:-5].max())
            recent_max = float(high_252.iloc[-5:].max())
            made_new_high = bool(recent_max >= prior_max and prior_max > 0)
        else:
            made_new_high = False

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
        bullish_streak = TechnicalSignals.consecutive_bullish_candles(open_s, close, 10)
        avg_upper_wick, avg_lower_wick = TechnicalSignals.avg_wick_ratio(open_s, high, low, close, n=10)
        short_upper = avg_upper_wick <= 0.35
        short_lower = avg_lower_wick <= 0.35

        sigs = [
            # 【位置】高値圏
            Signal("52週高値乖離 ≤ 5%", f"{gap * 100:.2f}%", "≤5.00%", is_near),
            Signal("直近5日で52週高値を更新", "更新あり" if made_new_high else "未更新", "更新あり", made_new_high),
            Signal(
                "200日MA上抜け",
                f"{((latest_close - sma200_val) / sma200_val * 100):+.2f}%" if sma200_val else "N/A",
                ">0%",
                above_sma200,
            ),
            # 【トレンド】じわじわ上昇
            Signal("連続上昇日数 ≥3日（+0.5〜+3%）", f"{up_days}日", "≥3日", up_days >= 3),
            Signal("連続陽線 ≥3本", f"{bullish_streak}本", "≥3本", bullish_streak >= 3),
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
            "near_high", "new_high", "above_sma200",
            "up_days", "bullish_streak", "ret_5d", "vol_increase",
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
    needs_fundamentals = True
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
    needs_fundamentals = True
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


def evaluate_fundamental_gate(fundamentals: Optional[dict]) -> dict:
    """決算分析の地図（村上茂久）の「会計視点×ファイナンス視点の両方を持つ」を、
    get_fundamentals の値だけで 1 銘柄ぶん判定する決定論的スコアカード。

    FundamentalGateStrategy（厳格 AND ゲート）と analyze_position（ポジション診断の
    ソフト判定）の双方がこの関数を唯一の真実として共有する。

    返り値の checks 各要素: {key, name, value, threshold, view, passed, available}
    """
    f = fundamentals or {}
    roe = f.get("roe")
    opm = f.get("operating_margin")
    rev = f.get("revenue_growth")
    earn = f.get("earnings_growth")
    per = f.get("per") or f.get("forward_per")
    ACC, FIN = "会計視点", "ファイナンス視点"

    def _pct(v):
        return f"{v * 100:.2f}%" if v is not None else "N/A"

    checks = [
        {"key": "roe", "name": "ROE", "value": _pct(roe), "threshold": "≥8%", "view": ACC,
         "passed": bool(roe is not None and roe >= 0.08), "available": roe is not None},
        {"key": "op_margin", "name": "営業利益率", "value": _pct(opm), "threshold": "≥8%", "view": ACC,
         "passed": bool(opm is not None and opm >= 0.08), "available": opm is not None},
        {"key": "revenue_growth", "name": "売上成長率", "value": _pct(rev), "threshold": "≥5%", "view": FIN,
         "passed": bool(rev is not None and rev >= 0.05), "available": rev is not None},
        {"key": "earnings_quality", "name": "利益成長率", "value": _pct(earn), "threshold": "≥0%", "view": ACC,
         "passed": bool(earn is not None and earn >= 0.0), "available": earn is not None},
        {"key": "valuation", "name": "PER", "value": (f"{per:.2f}" if per else "N/A"),
         "threshold": "0<PER≤30", "view": FIN,
         "passed": bool(per and 0 < per <= 30), "available": per is not None},
    ]
    available = sum(1 for c in checks if c["available"])
    passed = sum(1 for c in checks if c["passed"])
    total = len(checks)
    score = round(passed / total * 100, 1)
    # ソフト判定: 取得できた指標の 6 割以上を通過（最低 3 指標は必要）
    ok = bool(available >= 3 and (passed / available) >= 0.6)
    return {"checks": checks, "passed": passed, "available": available,
            "total": total, "score": score, "ok": ok}


def evaluate_safety(financials: Optional[dict]) -> Optional[dict]:
    """EDINET 財務サマリーから「安全性/キャッシュ」（決算分析の地図 2〜3章）を評価する。

    会計の利益だけでなく、自己資本比率（B/Sの安全性）・営業CF/FCF（本業のキャッシュ創出）
    まで踏み込む。financials が無ければ None。
    """
    if not financials or not financials.get("ok"):
        return None
    eq = financials.get("equity_ratio")
    ocf = financials.get("operating_cf")
    fcf = financials.get("fcf")

    def _cf(v):
        if v is None:
            return "N/A"
        return f"{'+' if v >= 0 else '−'}{abs(v):,.0f}"

    checks = [
        {"key": "equity_ratio", "name": "自己資本比率",
         "value": f"{eq * 100:.1f}%" if eq is not None else "N/A", "threshold": "≥30%",
         "passed": bool(eq is not None and eq >= 0.30), "available": eq is not None},
        {"key": "operating_cf", "name": "営業CF", "value": _cf(ocf), "threshold": "プラス",
         "passed": bool(ocf is not None and ocf > 0), "available": ocf is not None},
        {"key": "fcf", "name": "FCF", "value": _cf(fcf), "threshold": "プラス",
         "passed": bool(fcf is not None and fcf > 0), "available": fcf is not None},
    ]
    available = sum(1 for c in checks if c["available"])
    passed = sum(1 for c in checks if c["passed"])
    score = round(passed / len(checks) * 100, 1)
    ok = bool(available >= 2 and (passed / available) >= 0.5)
    return {
        "checks": checks, "passed": passed, "available": available, "score": score, "ok": ok,
        "equity_ratio": eq, "operating_cf": ocf, "fcf": fcf,
        "cs_pattern": financials.get("cs_pattern"),
        "period_end": financials.get("period_end"),
        "source": financials.get("source", "EDINET"),
    }


class FundamentalGateStrategy(StyleStrategy):
    """決算分析の地図（村上茂久）の「会計視点×ファイナンス視点の両方を持つ」
    を 1 銘柄判定に落とした、決定論的ファンダ・ゲート。

    テクニカル（じわじわ高値ブレイク等）と combine_mode="all" で AND 結合し、
    「テクニカルでもファンダでも買い」の二重ゲートを作るための土台。
    """
    style_name = "fundamental_gate"
    display_name = "ファンダ・ゲート（決算分析の地図）"
    description = "会計視点(収益性・資本効率)とファイナンス視点(成長性・割安性)の両面が揃った銘柄"
    needs_fundamentals = True
    filters = [
        FilterDef("roe", "ROE ≥ 8%", "資本効率（伊藤レポート基準）", True),
        FilterDef("op_margin", "営業利益率 ≥ 8%", "本業の収益性（会計視点）", True),
        FilterDef("revenue_growth", "売上成長率 ≥ 5%", "トップライン成長（未来）", True),
        FilterDef("earnings_quality", "利益成長率 ≥ 0%", "黒字かつ利益が伸びている", True),
        FilterDef("valuation", "PER 妥当域 (0<PER≤30)", "割高すぎない（時価総額）", True),
    ]

    def evaluate(self, code, name, sector, df, fundamentals=None, enabled_filters=None, near_miss=False):
        if not fundamentals:
            return None
        enabled = self._resolve_enabled(enabled_filters)
        gate = evaluate_fundamental_gate(fundamentals)
        sigs, signal_keys = [], []
        for c in gate["checks"]:
            sigs.append(Signal(
                c["name"], c["value"], c["threshold"], c["passed"],
                f"yfinance fundamentals（{c['view']}）",
            ))
            signal_keys.append(c["key"])
        return self._finalize(code, name, sector, sigs, enabled, signal_keys, df, near_miss=near_miss)


STRATEGY_REGISTRY: dict[str, StyleStrategy] = {
    s.style_name: s for s in [
        CreepingBreakoutStrategy(),
        ValueStrategy(),
        GrowthStrategy(),
        FundamentalGateStrategy(),
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
            "needs_fundamentals": s.needs_fundamentals,
            "filters": s.list_filters(),
        }
        for s in STRATEGY_REGISTRY.values()
    ]


# =========================================================
# 上昇余地・利確目標プロジェクション（決定論的・Gemini非依存）
# =========================================================
def analyze_breakout_projection(
    df,
    *,
    breakout_lookback: int = 60,
    horizon: int = 60,
    atr_n: int = 14,
    leg_window: int = 120,
) -> dict:
    """過去の「N日高値ブレイク」後の値動きから、この先の上昇余地・利確目標・損切り目安を
    統計的に推定する。Gemini を使わず OHLCV のみで計算するためハルシネーションがない。

    手順:
      1. 過去に breakout_lookback 日高値を更新した日（ブレイク）を抽出
      2. 各ブレイク後 horizon 日のピーク上昇率・到達日数・最大ドローダウンを集計
         → 上昇率の四分位（控えめ/中央/強気）と「天井までの日数」中央値
      3. 現在の上昇レッグ（直近 leg_window 日の最安値を起点）と既上昇率を測定
      4. 統計・ATR・計測ムーブ（スイング幅延伸）から利確目標を3段階で合成
      5. 損切り目安（起点 or 2ATR 下）とリスクリワード、残り上昇余地を算出
    """
    import math
    import numpy as np

    if df is None or len(df) < 250:
        return {"ok": False, "error": "分析に十分な履歴がありません（約1年以上必要）"}

    close = df["Close"].astype(float).to_numpy()
    high = df["High"].astype(float).to_numpy()
    low = df["Low"].astype(float).to_numpy()
    n = len(close)
    atr_arr = TechnicalSignals.atr(df["High"], df["Low"], df["Close"], n=atr_n).to_numpy()

    def _fin(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None

    # --- 1〜2. 過去ブレイクの検出と前向き成績 ---
    gains: list[float] = []
    days_to_peak: list[int] = []
    drawdowns: list[float] = []  # エントリー基準の最悪含み損（負値）
    last_bo = -(10 ** 9)
    min_gap = max(10, breakout_lookback // 3)
    for i in range(breakout_lookback, n - horizon - 1):
        prior_high = high[i - breakout_lookback:i].max()
        if not (high[i] > prior_high):
            continue
        if i - last_bo < min_gap:  # 連続ブレイクのクラスタを1件に圧縮
            continue
        last_bo = i
        entry = close[i]
        if not (entry > 0):
            continue
        fwd_high = high[i + 1:i + 1 + horizon]
        fwd_low = low[i + 1:i + 1 + horizon]
        if fwd_high.size == 0:
            continue
        peak = float(fwd_high.max())
        gains.append((peak - entry) / entry)
        days_to_peak.append(int(fwd_high.argmax()) + 1)
        drawdowns.append((float(fwd_low.min()) - entry) / entry)

    sample = len(gains)

    def _pct(arr, q):
        return float(np.percentile(arr, q)) if arr else None

    g25, g50, g75 = _pct(gains, 25), _pct(gains, 50), _pct(gains, 75)
    dd50 = _pct(drawdowns, 50)
    days50 = _pct(days_to_peak, 50)

    # --- 3. 現在の上昇レッグ ---
    last_close = _fin(close[-1])
    last_atr = None
    for k in range(1, min(6, n) + 1):
        last_atr = _fin(atr_arr[-k])
        if last_atr:
            break
    lw = min(leg_window, n)
    origin_rel = int(low[-lw:].argmin())
    origin_idx = n - lw + origin_rel
    origin_low = _fin(low[origin_idx])
    days_in_run = n - 1 - origin_idx
    current_gain = ((last_close - origin_low) / origin_low) if (origin_low and origin_low > 0) else 0.0
    swing_high = _fin(high[origin_idx:].max())
    high_252 = float(high[-252:].max()) if n >= 252 else float(high.max())
    gap_to_52w = ((high_252 - last_close) / high_252) if high_252 > 0 else 0.0

    try:
        origin_date = df.index[origin_idx].strftime("%Y-%m-%d")
    except Exception:
        origin_date = ""
    try:
        as_of = df.index[-1].strftime("%Y-%m-%d")
    except Exception:
        as_of = ""

    # --- 4. 利確目標の候補を集める ---
    cand_t1: list[float] = []  # 控えめ
    cand_t2: list[float] = []  # 中央
    cand_t3: list[float] = []  # 強気
    if origin_low and origin_low > 0:
        if g25 is not None:
            cand_t1.append(origin_low * (1 + g25))
        if g50 is not None:
            cand_t2.append(origin_low * (1 + g50))
        if g75 is not None:
            cand_t3.append(origin_low * (1 + g75))
        swing = (swing_high - origin_low) if swing_high else 0.0
        if swing > 0:
            cand_t1.append(origin_low + swing * 1.0)      # 1:1 計測ムーブ
            cand_t2.append(origin_low + swing * 1.272)
            cand_t3.append(origin_low + swing * 1.618)
    if last_atr:
        cand_t1.append(last_close + 2 * last_atr)
        cand_t2.append(last_close + 3 * last_atr)
        cand_t3.append(last_close + 4 * last_atr)

    def _consolidate(vals):
        vals = [v for v in vals if v and v > last_close]
        return float(np.median(vals)) if vals else None

    t1, t2, t3 = _consolidate(cand_t1), _consolidate(cand_t2), _consolidate(cand_t3)
    # 単調性を担保（T1<T2<T3）
    levels = [v for v in (t1, t2, t3) if v]
    levels = sorted(set(round(v, 1) for v in levels))
    target_meta = [("T1 控えめ", "ATR2倍・過去ブレイク下位25%・1:1計測"),
                   ("T2 本命", "ATR3倍・過去ブレイク中央値・1.272延伸"),
                   ("T3 強気", "ATR4倍・過去ブレイク上位25%・1.618延伸")]
    targets = []
    for idx, price in enumerate(levels[:3]):
        label, basis = target_meta[idx] if idx < len(target_meta) else (f"T{idx+1}", "")
        targets.append({
            "label": label,
            "price": round(price, 1),
            "upside_pct": round((price - last_close) / last_close * 100, 1) if last_close else None,
            "basis": basis,
        })

    # --- 5. 損切り・リスクリワード・残り上昇余地 ---
    stop = None
    if last_atr and last_close:
        atr_stop = last_close - 2 * last_atr
        # 損切りは「現値に近い戦術的水準」。直近15営業日のスイング安値を基本とし、
        # 現値から 1〜3ATR の範囲にある時だけ採用（近すぎ＝R/R過大、遠すぎ＝損大 を回避）。
        # それ以外は一律 2ATR 下に置く。※120日起点は上昇率の測定専用で損切りには使わない。
        recent_low = _fin(low[-15:].min()) if n >= 15 else None
        if recent_low and (last_atr <= (last_close - recent_low) <= 3 * last_atr):
            stop = recent_low
        else:
            stop = atr_stop
        if stop >= last_close:
            stop = last_close - 2 * last_atr
    risk_pct = round((last_close - stop) / last_close * 100, 1) if (stop and last_close) else None
    base_target = targets[1]["price"] if len(targets) >= 2 else (targets[0]["price"] if targets else None)
    rr = None
    if base_target and stop and last_close and (last_close - stop) > 0:
        rr = round((base_target - last_close) / (last_close - stop), 2)

    remaining_pct = None
    if g50 is not None:
        remaining_pct = round(max(0.0, (g50 - current_gain)) * 100, 1)

    # --- 判定文・注記 ---
    notes: list[str] = []
    if sample < 3:
        notes.append(f"過去のブレイク事例が{sample}件と少なく、統計の信頼度は低めです。ATR・計測ムーブ中心の目安です。")
    else:
        notes.append(f"過去{sample}回のブレイク後、中央値で約{g50 * 100:.0f}%上昇・天井まで約{days50:.0f}営業日。最悪含み損は中央値約{dd50 * 100:.0f}%。")
    if gap_to_52w is not None:
        if gap_to_52w <= 0.01:
            notes.append("現在ほぼ52週高値圏（上に過去の戻り売り圧力が少なく伸びやすい）。")
        else:
            notes.append(f"52週高値まであと約{gap_to_52w * 100:.1f}%（直近高値が当面の上値メド）。")
    if remaining_pct is not None:
        if current_gain >= (g50 or 0):
            notes.append(f"現レッグは起点から約{current_gain * 100:.0f}%上昇で、過去中央値（{(g50 or 0) * 100:.0f}%）を既に超過。利益確定を意識する局面。")
        else:
            notes.append(f"現レッグは起点から約{current_gain * 100:.0f}%上昇。過去中央値まで概算で残り約{remaining_pct:.0f}%の余地。")

    # 総合判定（ざっくり）
    if rr is not None and rr >= 2 and (current_gain < (g50 or 0)):
        verdict = "妙味あり：リスクリワード良好で、過去の典型的な上昇余地もまだ残る。"
    elif current_gain >= (g75 or 1e9):
        verdict = "過熱気味：過去ブレイクの上位25%水準まで上昇済み。新規は慎重に、保有分は利確検討。"
    elif rr is not None and rr < 1:
        verdict = "見送り寄り：直近高値までの距離が近く、損切り幅に対する利幅が小さい。"
    else:
        verdict = "中立：T1（控えめ目標）での部分利確を起点に、トレンド継続を見ながら。"

    return {
        "ok": True,
        "as_of": as_of,
        "last_close": round(last_close, 1) if last_close else None,
        "atr": round(last_atr, 2) if last_atr else None,
        "atr_pct": round(last_atr / last_close * 100, 2) if (last_atr and last_close) else None,
        "leg": {
            "origin_low": round(origin_low, 1) if origin_low else None,
            "origin_date": origin_date,
            "days_in_run": days_in_run,
            "current_gain_pct": round(current_gain * 100, 1),
            "swing_high": round(swing_high, 1) if swing_high else None,
        },
        "history": {
            "sample": sample,
            "gain_p25_pct": round(g25 * 100, 1) if g25 is not None else None,
            "gain_p50_pct": round(g50 * 100, 1) if g50 is not None else None,
            "gain_p75_pct": round(g75 * 100, 1) if g75 is not None else None,
            "days_to_peak_p50": round(days50) if days50 is not None else None,
            "drawdown_p50_pct": round(dd50 * 100, 1) if dd50 is not None else None,
        },
        "high_52w": round(high_252, 1) if high_252 else None,
        "gap_to_52w_pct": round(gap_to_52w * 100, 1),
        "targets": targets,
        "stop": {"price": round(stop, 1) if stop else None, "risk_pct": risk_pct},
        "risk_reward": rr,
        "remaining_estimate_pct": remaining_pct,
        "verdict": verdict,
        "notes": notes,
    }


# =========================================================
# ポジション診断（保有継続/売却・新規買い判定）— 決定論的
# =========================================================
_ACTION_LABELS = {
    "HOLD": "継続保有",
    "HOLD_WATCH": "保有（ファンダ警戒）",
    "TRIM": "一部利確・縮小",
    "SELL": "売却・撤退",
    "BUY": "新規買い候補",
    "WATCH": "ウォッチ（見送り）",
}
_STATE_LABELS = {
    "uptrend": "上昇トレンド",
    "neutral": "横ばい・調整",
    "broken": "トレンド崩れ",
}


def analyze_position(
    df,
    fundamentals: Optional[dict] = None,
    *,
    avg_cost: Optional[float] = None,
    held: bool = True,
    financials: Optional[dict] = None,
    sma_fast: int = 25,
    sma_mid: int = 75,
    sma_slow: int = 200,
) -> dict:
    """1 銘柄について「テクニカルのトレンド状態 × ファンダの健全性」を決定論的に評価し、
    継続保有 / 縮小 / 売却（保有銘柄）または 新規買い / 見送り（候補）の判定を返す。

    利確の思想:
      高値ブレイクかつファンダ健全な銘柄は「基本的に上昇するものとして継続保有」し、
      出口は固定の利確目標ではなく「トレンド崩れ（トレイリングストップ割れ）または
      ファンダ悪化」で手仕舞う —— という順張り×ファンダのセオリーを実装する。
      過去の値動きから出した利確目標(analyze_breakout_projection)は、ここでは
      「ストップ位置・到達余地の参考値」であって機械的な売りトリガーではない。
    """
    import math

    if df is None or len(df) < 60:
        return {"ok": False, "error": "トレンド判定に十分な履歴がありません（約3ヶ月以上必要）"}

    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    n = len(close)

    def _fin(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None

    last_close = _fin(close.iloc[-1])
    if last_close is None or last_close <= 0:
        return {"ok": False, "error": "価格データが欠損しています"}

    sma_f = _fin(TechnicalSignals.sma(close, sma_fast).iloc[-1]) if n >= sma_fast else None
    sma_m = _fin(TechnicalSignals.sma(close, sma_mid).iloc[-1]) if n >= sma_mid else None
    sma_s = _fin(TechnicalSignals.sma(close, sma_slow).iloc[-1]) if n >= sma_slow else None

    atr_series = TechnicalSignals.atr(high, low, close, n=14)
    last_atr = None
    for k in range(1, min(6, n) + 1):
        last_atr = _fin(atr_series.iloc[-k])
        if last_atr:
            break

    # トレイリングストップ: シャンデリア・エグジット（直近22日高値 − 3ATR）
    hh22 = _fin(high.tail(22).max())
    trailing_stop = (hh22 - 3 * last_atr) if (hh22 and last_atr) else None
    below_stop = bool(trailing_stop and last_close < trailing_stop)

    hh60 = _fin(high.tail(60).max())
    drawdown = ((last_close - hh60) / hh60) if (hh60 and hh60 > 0) else 0.0
    high252 = _fin(high.tail(252).max()) if n >= 252 else _fin(high.max())
    gap52 = ((high252 - last_close) / high252) if (high252 and high252 > 0) else 0.0

    above_fast = bool(sma_f and last_close > sma_f)
    above_mid = bool(sma_m and last_close > sma_m)
    perfect_order = bool(
        sma_f and sma_m and last_close > sma_f > sma_m
        and (sma_s is None or sma_m > sma_s)
    )

    conds = [
        above_fast,
        above_mid,
        (sma_s is None or (sma_m and sma_s and sma_m > sma_s)),
        (not below_stop),
        (gap52 is not None and gap52 <= 0.05),
        perfect_order,
    ]
    trend_score = round(sum(1 for c in conds if c) / len(conds) * 100, 1)

    if below_stop or (sma_m and last_close < sma_m) or drawdown <= -0.18:
        state = "broken"
    elif above_fast and above_mid:
        state = "uptrend"
    else:
        state = "neutral"

    has_fund = bool(fundamentals)
    gate = evaluate_fundamental_gate(fundamentals) if has_fund else None
    fund_ok = gate["ok"] if (gate and gate["available"] >= 3) else None  # None=判定不能
    fund_score = gate["score"] if gate else None

    safety = evaluate_safety(financials)
    safety_ok = safety["ok"] if safety else None
    safety_score = safety["score"] if safety else None
    # 本業でキャッシュを稼げていない（営業CFがマイナス）かどうか
    ocf_negative = bool(safety and safety.get("operating_cf") is not None and safety["operating_cf"] <= 0)

    # 含み損益
    pnl = None
    if avg_cost:
        try:
            ac = float(avg_cost)
            if ac > 0:
                pnl = {"avg_cost": round(ac, 2),
                       "pnl_pct": round((last_close - ac) / ac * 100, 1)}
        except (TypeError, ValueError):
            pnl = None

    # --- 判定 ---
    reasons = []
    reasons.append(f"トレンド: {_STATE_LABELS[state]}（"
                   f"{'25MA上' if above_fast else '25MA下'}/"
                   f"{'75MA上' if above_mid else '75MA下'}"
                   f"{'・パーフェクトオーダー' if perfect_order else ''}）")
    if has_fund:
        reasons.append(f"ファンダ: {gate['passed']}/{gate['total']}通過（{gate['score']}点）"
                       f"・{'健全' if fund_ok else ('要警戒' if fund_ok is False else '判定不能')}")
    else:
        reasons.append("ファンダ: データ未取得")
    if safety:
        eqv = safety.get("equity_ratio")
        parts = []
        parts.append(f"自己資本比率{eqv * 100:.0f}%" if eqv is not None else "自己資本比率N/A")
        if safety.get("cs_pattern"):
            parts.append(safety["cs_pattern"])
        if safety.get("fcf") is not None:
            parts.append("FCF＋" if safety["fcf"] > 0 else "FCF−")
        reasons.append("財務(EDINET): " + "・".join(parts))
    if pnl:
        reasons.append(f"含み{'益' if pnl['pnl_pct'] >= 0 else '損'} {pnl['pnl_pct']:+.1f}%")

    note = ""
    if held:
        if state == "uptrend":
            if fund_ok is False:
                action = "HOLD_WATCH"
                note = "トレンドは継続中だがファンダに陰り。トレンドが崩れたら速やかに手仕舞い。"
            else:
                action = "HOLD"
                note = "高値追随かつファンダ健全。利を伸ばす局面。トレイリングストップを切り上げて防御。"
        elif state == "neutral":
            if fund_ok is False:
                action = "TRIM"
                note = "横ばい＋ファンダ悪化。資金効率の観点から縮小し、強い銘柄へ。"
            else:
                action = "HOLD"
                note = "横ばい・調整。トレイリングストップを意識しつつ様子見。"
        else:  # broken
            if fund_ok is False:
                action = "SELL"
                note = "トレンド崩れ＋ファンダ悪化の両方ダメ。撤退を優先。"
            elif below_stop:
                action = "SELL"
                note = "トレイリングストップ割れ。ファンダは健全なので、押し目を作れば再エントリー候補。"
            else:
                action = "TRIM"
                note = "トレンドが崩れ気味。一部利確で防御。ファンダ健全のため全売りは急がない。"
    else:  # 新規候補
        if state == "uptrend" and fund_ok is True:
            action = "BUY"
            note = "テクニカル（上昇トレンド）とファンダ（健全）の両方で買い。R/R・ストップは利確目安(projection)を参照。"
        elif state == "uptrend" and fund_ok is None:
            action = "WATCH"
            note = "トレンドは良好だがファンダ未取得/判定不能。ファンダ確認後に判断。"
        elif state == "uptrend":
            action = "WATCH"
            note = "トレンドは良好だがファンダが基準未達。テクニカル単独では見送り。"
        else:
            action = "WATCH"
            note = "トレンド未確立。高値ブレイクの確認待ち。"

    # --- 安全性（EDINET財務）による上書き ---
    if safety is not None:
        if not held and action == "BUY" and (ocf_negative or safety_ok is False):
            action = "WATCH"
            note = ("テクニカル・ファンダは良好だが、財務（EDINET）に懸念"
                    f"（{'本業CFがマイナス' if ocf_negative else '自己資本比率/FCFが基準未達'}）。"
                    "本業でキャッシュを稼げているか確認してから。")
        elif held and action == "HOLD" and safety_ok is False:
            action = "HOLD_WATCH"
            note = "トレンドは良好だが財務（EDINET）に懸念。自己資本比率・営業CFの推移を注視。"
        elif held and action in ("SELL", "TRIM") and ocf_negative:
            note += "（財務面でも営業CFがマイナスで撤退を支持）"

    if has_fund and fund_score is not None and safety_score is not None:
        score = round(0.45 * trend_score + 0.4 * fund_score + 0.15 * safety_score, 1)
    elif has_fund and fund_score is not None:
        score = round(0.5 * trend_score + 0.5 * fund_score, 1)
    else:
        score = trend_score

    # 相対比較（宝石5）用の生指標。PER は 0 以下を比較対象外にする。
    fm = fundamentals or {}
    per_raw = _fin(fm.get("per") or fm.get("forward_per"))
    metrics = {
        "roe": _fin(fm.get("roe")),
        "op_margin": _fin(fm.get("operating_margin")),
        "revenue_growth": _fin(fm.get("revenue_growth")),
        "earnings_growth": _fin(fm.get("earnings_growth")),
        "per": per_raw if (per_raw and per_raw > 0) else None,
        "trend_score": trend_score,
    }

    return {
        "ok": True,
        "as_of": StyleStrategy._data_as_of(df),
        "last_close": round(last_close, 1),
        "atr": round(last_atr, 2) if last_atr else None,
        "score": score,
        "trend": {
            "state": state,
            "state_label": _STATE_LABELS[state],
            "score": trend_score,
            "sma25": round(sma_f, 1) if sma_f else None,
            "sma75": round(sma_m, 1) if sma_m else None,
            "sma200": round(sma_s, 1) if sma_s else None,
            "perfect_order": perfect_order,
            "above_fast": above_fast,
            "above_mid": above_mid,
            "drawdown_from_peak_pct": round(drawdown * 100, 1),
            "gap_to_52w_pct": round(gap52 * 100, 1),
            "trailing_stop": round(trailing_stop, 1) if trailing_stop else None,
            "below_trailing_stop": below_stop,
        },
        "fundamental": gate,
        "fund_ok": fund_ok,
        "safety": safety,
        "metrics": metrics,
        "pnl": pnl,
        "verdict": {
            "action": action,
            "action_label": _ACTION_LABELS[action],
            "conviction": score,
            "reasons": reasons,
            "note": note,
        },
    }


# =========================================================
# 相対評価（宝石5：時系列・他社比較）— 決定論的
# =========================================================
_RELATIVE_METRICS = [
    ("roe", False, "ROE"),
    ("op_margin", False, "営業利益率"),
    ("revenue_growth", False, "売上成長"),
    ("earnings_growth", False, "利益成長"),
    ("per", True, "割安(PER)"),       # PER は小さいほど良い
    ("trend_score", False, "トレンド強さ"),
]


def compute_relative_metrics(results: list[dict]) -> list[dict]:
    """analyze_position の結果群を「他社比較」で相対評価する（宝石5）。

    各銘柄の指標を、同セクター内（そのセクターに3社以上いる場合）または全体の
    ピア集合に対する百分位（パーセンタイル）でランク付けし、強み/弱みと
    相対スコアを付与する。さらに絶対スコアと相対スコアを 7:3 で混ぜた
    blended_score を加える（並べ替え・入替提案に使う）。in place で enrich。

    「比較してはじめて意味が出る」（決算分析の地図 宝石5）を、バッチ内の
    銘柄同士のクロスセクション比較として近似する。
    """
    from collections import defaultdict

    ok = [r for r in results if r.get("ok") and r.get("metrics")]
    if len(ok) < 2:
        for r in results:
            r.setdefault("blended_score", r.get("score"))
        return results

    by_sector: dict[str, list[dict]] = defaultdict(list)
    for r in ok:
        by_sector[(r.get("sector") or "").strip()].append(r)

    def _peer_set(r):
        sec = (r.get("sector") or "").strip()
        grp = by_sector.get(sec, [])
        if sec and len(grp) >= 3:
            return grp, "セクター内", sec
        return ok, "全体", ""

    def _values(group, key):
        return [g["metrics"].get(key) for g in group if g["metrics"].get(key) is not None]

    def _pctl(vals, x, lower_better):
        if not vals:
            return None
        if lower_better:
            cnt = sum(1 for v in vals if v >= x)
        else:
            cnt = sum(1 for v in vals if v <= x)
        return round(cnt / len(vals) * 100)

    for r in ok:
        group, glabel, sec = _peer_set(r)
        m = r["metrics"]
        pers: dict[str, Optional[int]] = {}
        for key, lb, _label in _RELATIVE_METRICS:
            x = m.get(key)
            pers[key] = _pctl(_values(group, key), x, lb) if x is not None else None
        avail = [v for v in pers.values() if v is not None]
        rel_score = round(sum(avail) / len(avail), 1) if avail else None

        # 強み（上位25%）と弱み（下位25%）を抽出。上位ほど前に。
        ranked = sorted(
            [(key, label, pers[key]) for key, _lb, label in _RELATIVE_METRICS if pers[key] is not None],
            key=lambda t: t[2], reverse=True,
        )
        highlights = [f"{glabel} {label} 上位{max(1, 100 - p)}%" for _k, label, p in ranked if p >= 75][:3]
        laggards = [f"{label} 下位{max(1, p)}%" for _k, label, p in sorted(ranked, key=lambda t: t[2]) if p <= 25][:2]

        r["relative"] = {
            "group": glabel,
            "group_name": sec,
            "peer_n": len(group),
            "percentiles": pers,
            "score": rel_score,
            "highlights": highlights,
            "laggards": laggards,
        }
        base = r.get("score") or 0
        r["blended_score"] = round(0.7 * base + 0.3 * rel_score, 1) if rel_score is not None else base

    for r in results:
        r.setdefault("blended_score", r.get("score"))
    return results
