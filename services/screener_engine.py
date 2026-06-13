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
    def wick_stats(open_s, high, low, close, n: int = 10) -> tuple[float, float, float]:
        """直近 n 本の (上ひげ・下ひげのレンジ加重平均比, 最長の単独上ひげ比) を返す。

        上ひげ比 = (high - max(open, close)) / (high - low)
        下ひげ比 = (min(open, close) - low)  / (high - low)

        単純平均だとレンジが極小の足（ほぼ寄引同事）でひげ比が過大に出て平均を歪める。
        そこで「Σひげ ÷ Σレンジ」のレンジ加重平均にして、実体の大きい（＝意味のある）
        足を重く扱う。加えて、直近 n 本で最も長い単独の上ひげ比（突出した戻り売り＝
        天井サイン）も返し、平均では薄まる一本値を別途チェックできるようにする。
        """
        import pandas as pd  # type: ignore
        if len(close) < n or len(open_s) < n:
            return 0.0, 0.0, 0.0
        o = open_s.tail(n).reset_index(drop=True)
        h = high.tail(n).reset_index(drop=True)
        l = low.tail(n).reset_index(drop=True)
        c = close.tail(n).reset_index(drop=True)
        rng = (h - l)
        body_high = pd.concat([o, c], axis=1).max(axis=1)
        body_low = pd.concat([o, c], axis=1).min(axis=1)
        upper_abs = (h - body_high)
        lower_abs = (body_low - l)
        total_rng = float(rng.sum())
        if total_rng <= 0:
            return 0.0, 0.0, 0.0
        u = float(upper_abs.sum()) / total_rng
        d = float(lower_abs.sum()) / total_rng
        # 単独上ひげ比は、レンジが極小の足のノイズを避けるため
        # レンジが直近平均の30%以上ある足だけを対象にする。
        avg_rng = total_rng / n
        per_candle = (upper_abs / rng.replace(0, float("nan")))
        mask = rng >= (avg_rng * 0.30)
        sel = per_candle[mask].dropna()
        u_max = float(sel.max()) if len(sel) else u
        return u, d, u_max

    # --- チャートパターン検出（DUKE『新高値ブレイク投資法』4章）---
    # いずれもヒューリスティック近似。パターンは本質的に曖昧なので、保守的に判定する。

    @staticmethod
    def detect_box_breakout(close, high, low, volume, *, window: int = 80,
                            box_tol: float = 0.20, vol_mult: float = 1.3) -> dict:
        """ボックス（持ち合い）を上抜けたかを判定する。

        直近 window 日のうち末尾3日を除いた区間を「ボックス」とみなし、値幅が box_tol
        以内に収まる横ばいなら、最新終値がボックス上限を上抜け＋出来高増加でブレイク成立。
        """
        n = len(close)
        if n < window + 5:
            return {"detected": False}
        region_high = high.iloc[-window:-3]
        region_low = low.iloc[-window:-3]
        box_high = float(region_high.max())
        box_low = float(region_low.min())
        if not (box_high > 0 and box_low > 0):
            return {"detected": False}
        box_range = (box_high - box_low) / box_low
        tight = box_range <= box_tol
        last_close = float(close.iloc[-1])
        broke = last_close > box_high
        vr = TechnicalSignals.latest_volume_vs_avg(volume, 20)
        detected = bool(tight and broke and vr >= vol_mult)
        return {
            "detected": detected, "box_high": round(box_high, 2), "box_low": round(box_low, 2),
            "box_range_pct": round(box_range * 100, 1), "vol_ratio": round(vr, 2),
        }

    @staticmethod
    def _swings(high, low, k: int = 3) -> list[tuple[int, float, str]]:
        """±k 本で極大/極小となる点を (位置, 価格, "H"/"L") で返す素朴なスイング検出。"""
        n = len(high)
        sw: list[tuple[int, float, str]] = []
        for i in range(k, n - k):
            hi = float(high.iloc[i])
            lo = float(low.iloc[i])
            if hi == float(high.iloc[i - k:i + k + 1].max()):
                sw.append((i, hi, "H"))
            elif lo == float(low.iloc[i - k:i + k + 1].min()):
                sw.append((i, lo, "L"))
        return sw

    @staticmethod
    def detect_vcp(close, high, low, volume, *, lookback: int = 70,
                   max_base_depth: float = 0.35, final_contraction_max: float = 0.12,
                   near_pivot_tol: float = 0.06) -> dict:
        """VCP（ボラティリティ収縮）を判定する。

        高値→安値の押し幅が2回以上連続で縮小し、最後の押しが浅く、現値が直近高値
        （ピボット）近辺にある状態を成立とする（ミネルヴィニ／ダーバス）。近似。
        """
        n = len(close)
        if n < lookback + 5:
            return {"detected": False}
        h = high.tail(lookback).reset_index(drop=True)
        l = low.tail(lookback).reset_index(drop=True)
        sw = TechnicalSignals._swings(h, l, k=3)
        depths: list[float] = []
        last_high = None
        for _idx, price, kind in sw:
            if kind == "H":
                last_high = price
            elif kind == "L" and last_high and last_high > 0:
                depths.append((last_high - price) / last_high)
                last_high = None
        if len(depths) < 2:
            return {"detected": False}
        recent = depths[-3:]
        contracting = all(recent[i] <= recent[i - 1] * 0.9 for i in range(1, len(recent)))
        final_shallow = recent[-1] <= final_contraction_max
        base_ok = max(recent) <= max_base_depth
        pivot = float(h.max())
        last_close = float(close.iloc[-1])
        near_pivot = bool(pivot > 0 and (pivot - last_close) / pivot <= near_pivot_tol)
        v = volume.tail(lookback)
        vol_dry = bool(len(v) >= 30 and float(v.tail(10).mean()) <= float(v.tail(30).head(20).mean()))
        detected = bool(contracting and final_shallow and base_ok and near_pivot)
        return {
            "detected": detected,
            "contractions_pct": [round(d * 100, 1) for d in recent],
            "pivot": round(pivot, 2), "near_pivot": near_pivot, "vol_dry": vol_dry,
        }

    @staticmethod
    def detect_cup_with_handle(close, high, low, volume, *, max_cup: int = 180,
                               min_depth: float = 0.12, max_depth: float = 0.50,
                               handle_max_depth: float = 0.15, near_pivot_tol: float = 0.07) -> dict:
        """カップウィズハンドルを判定する。

        左縁→底→右縁（左縁付近まで回復）で丸いカップを作り、右縁近くで浅い押し（ハンドル）
        を経て、現値がピボット（ハンドル高値≒左縁）近辺/上にある状態（オニール）。近似。
        """
        n = len(close)
        if n < 38:
            return {"detected": False}
        span = min(max_cup, n - 1)
        H = high.tail(span).reset_index(drop=True)
        L = low.tail(span).reset_index(drop=True)
        C = close.tail(span).reset_index(drop=True)
        m = len(C)
        left_n = max(3, m // 7)
        left_rim_idx = int(H.iloc[:left_n].idxmax())
        left_rim = float(H.iloc[:left_n].max())
        bottom_region = L.iloc[left_rim_idx + 1: m - left_n]
        if len(bottom_region) < 5 or left_rim <= 0:
            return {"detected": False}
        bottom = float(bottom_region.min())
        bottom_idx = int(bottom_region.idxmin())
        depth = (left_rim - bottom) / left_rim
        if not (min_depth <= depth <= max_depth):
            return {"detected": False}
        right_high = float(H.iloc[bottom_idx + 1:].max()) if bottom_idx + 1 < m else 0.0
        recovered = right_high >= left_rim * 0.93
        handle = C.tail(min(20, max(5, m // 4)))
        handle_high = float(handle.max())
        handle_low = float(handle.min())
        handle_depth = (handle_high - handle_low) / handle_high if handle_high > 0 else 1.0
        cup_mid = (left_rim + bottom) / 2
        handle_ok = bool(handle_depth <= handle_max_depth and handle_low >= cup_mid)
        pivot = max(handle_high, left_rim)
        last_close = float(close.iloc[-1])
        near_pivot = bool(pivot > 0 and (pivot - last_close) / pivot <= near_pivot_tol)
        detected = bool(recovered and handle_ok and near_pivot)
        return {
            "detected": detected, "depth_pct": round(depth * 100, 1),
            "handle_depth_pct": round(handle_depth * 100, 1),
            "pivot": round(pivot, 2), "near_pivot": near_pivot,
        }

    @staticmethod
    def detect_earnings_gap(open_s, close, volume, *, lookback: int = 12,
                            gap_min: float = 0.04, vol_mult: float = 1.5) -> dict:
        """直近 lookback 営業日内の「窓を開けた急騰（決算ギャップ）」を検出する。

        kenmo『5年で1億』の決算モメンタム投資の中核。yfinance に四半期サプライズが無いため、
        ギャップ率 =(始値 − 前日終値)/前日終値 と当日の出来高急増を「好決算サプライズ」の
        価格代理シグナルにする。最大ギャップの日を採用し、その後に終値を維持しているか
        （held）も返す。OHLCV のみで決定論的。
        """
        n = min(len(close), len(open_s), len(volume))
        if n < lookback + 21:
            return {"detected": False}
        best = None
        for j in range(1, lookback + 1):
            i = n - j
            if i < 21:
                break
            prev_c = float(close.iloc[i - 1])
            op = float(open_s.iloc[i])
            if prev_c <= 0 or op != op or prev_c != prev_c:
                continue
            gap = (op - prev_c) / prev_c
            vavg = float(volume.iloc[i - 20:i].mean())
            vr = (float(volume.iloc[i]) / vavg) if vavg > 0 else 0.0
            if gap >= gap_min and vr >= vol_mult:
                if best is None or gap > best["gap"]:
                    best = {"gap": gap, "days_ago": j, "vol_ratio": vr,
                            "gap_close": float(close.iloc[i])}
        if best is None:
            return {"detected": False}
        last_close = float(close.iloc[-1])
        held = bool(best["gap_close"] > 0 and last_close >= best["gap_close"] * 0.97)
        run_up = ((last_close - best["gap_close"]) / best["gap_close"]) if best["gap_close"] > 0 else 0.0
        return {
            "detected": True, "gap_pct": round(best["gap"] * 100, 1),
            "days_ago": best["days_ago"], "held": held,
            "vol_ratio": round(best["vol_ratio"], 2),
            "gap_close": round(best["gap_close"], 1),
            "run_up_pct": round(run_up * 100, 1),
        }


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
    # メソッドの大分類: "technical"（テクニカル）/ "fundamental"（ファンダ）/ "hybrid"（複合）
    category: str = "fundamental"
    # True の戦略は UI のメソッド一覧・採点比較から隠す（他メソッドの内部部品など）
    hidden: bool = False

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
    display_name = "新高値ブレイク（じわじわ・低ボラ）"
    description = "52週高値圏でじわじわ上昇し直近で高値を更新、急騰や下抜けがない銘柄"
    category = "technical"
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
        FilterDef("short_upper_wick", "上ひげ ≤ 35%（加重平均・突出なし）", "戻り売りに押されていない（天井サインの長い上ひげが無い）", True),
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
        avg_upper_wick, avg_lower_wick, max_upper_wick = TechnicalSignals.wick_stats(open_s, high, low, close, n=10)
        # 上ひげ: レンジ加重平均が小さく、かつ単独で突出した長い上ひげ(戻り売り＝天井サイン)も無いこと
        short_upper = (avg_upper_wick <= 0.35) and (max_upper_wick <= 0.60)
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
            Signal("上ひげ ≤ 35%（加重平均・突出なし）", f"平均{avg_upper_wick * 100:.0f}%/最長{max_upper_wick * 100:.0f}%", "平均≤35%かつ最長≤60%", short_upper),
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
    display_name = "バリュー（割安・配当）"
    description = "PER/PBR が低く、財務健全で配当も出ている銘柄"
    needs_fundamentals = True
    category = "fundamental"
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
    display_name = "グロース（成長）"
    description = "売上・利益成長率と ROE が高い銘柄"
    needs_fundamentals = True
    category = "fundamental"
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
    display_name = "ファンダ総合（村上茂久）"
    description = "会計視点(収益性・資本効率)とファイナンス視点(成長性・割安性)の両面が揃った銘柄"
    needs_fundamentals = True
    category = "fundamental"
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


class BreakoutPatternStrategy(StyleStrategy):
    """DUKE『新高値ブレイク投資法』4章の代表的チャート型（カップウィズハンドル・VCP・
    ボックスブレイク）を検出する順張りストラテジー。52週高値圏で型が成立した銘柄を拾う。
    """
    style_name = "breakout_patterns"
    display_name = "新高値ブレイク（チャート型）"
    description = "52週高値圏で、カップウィズハンドル・VCP・ボックスのいずれかのブレイク型が成立した銘柄"
    category = "technical"
    # 「新高値ブレイク投資術」(new_high_breakout) の内部部品。単体運用も可能だが、
    # 複合メソッドと重複するため一覧からは隠す（必要なら combine で名指し可能）。
    hidden = True
    filters = [
        FilterDef("near_high", "52週高値乖離 ≤ 10%", "高値圏に位置", True),
        FilterDef("above_sma200", "200日MA上抜け", "中長期の上昇トレンド", True),
        FilterDef("chart_pattern", "カップ/VCP/ボックスのいずれか成立", "代表的なブレイク型", True),
        FilterDef("vol_breakout", "出来高 ≥ 20日平均の1.3x", "ブレイク時の出来高増加", True),
    ]

    def evaluate(self, code, name, sector, df, fundamentals=None, enabled_filters=None, near_miss=False):
        if df is None or len(df) < 200:
            return None
        enabled = self._resolve_enabled(enabled_filters)
        close, high, low, volume = df["Close"], df["High"], df["Low"], df["Volume"]
        is_near, gap, _ = TechnicalSignals.near_52w_high(close, high, tolerance=0.10)
        sma200 = TechnicalSignals.sma(close, 200)
        sma200_val = float(sma200.iloc[-1]) if sma200.iloc[-1] == sma200.iloc[-1] else None
        last_close = float(close.iloc[-1])
        above_sma200 = bool(sma200_val and last_close > sma200_val)

        box = TechnicalSignals.detect_box_breakout(close, high, low, volume)
        vcp = TechnicalSignals.detect_vcp(close, high, low, volume)
        cup = TechnicalSignals.detect_cup_with_handle(close, high, low, volume)
        matched = [lbl for lbl, d in (("ボックス", box), ("VCP", vcp), ("カップ", cup)) if d.get("detected")]
        pattern_ok = len(matched) > 0
        vr = TechnicalSignals.latest_volume_vs_avg(volume, 20)

        sigs = [
            Signal("52週高値乖離 ≤ 10%", f"{gap * 100:.2f}%", "≤10.00%", is_near),
            Signal(
                "200日MA上抜け",
                f"{((last_close - sma200_val) / sma200_val * 100):+.2f}%" if sma200_val else "N/A",
                ">0%", above_sma200,
            ),
            Signal("チャート型成立", "・".join(matched) if matched else "なし", "いずれか成立", pattern_ok),
            Signal("出来高 ≥ 1.3x", f"{vr:.2f}x", "≥1.30x", vr >= 1.30),
        ]
        signal_keys = ["near_high", "above_sma200", "chart_pattern", "vol_breakout"]
        bonus = 10.0 * len(matched)  # 複数の型が同時成立するほど加点
        return self._finalize(code, name, sector, sigs, enabled, signal_keys, df, score_bonus=bonus, near_miss=near_miss)


def evaluate_growth_gate(fundamentals: Optional[dict]) -> dict:
    """DUKE『新高値ブレイク投資法』5章の強気な業績基準を get_fundamentals の値で近似判定する。

    本来は「営業利益 四半期 前年同期比 +20%以上」「売上 +10%以上」だが、yfinance からは
    四半期QoQの営業利益系列が取れないため、年次YoY成長率（revenue_growth/earnings_growth）と
    営業利益率・ROE の水準で代替する。決定論的スコアカード。
    """
    f = fundamentals or {}
    rev = f.get("revenue_growth")
    earn = f.get("earnings_growth")
    opm = f.get("operating_margin")
    roe = f.get("roe")

    def _pct(v):
        return f"{v * 100:.2f}%" if v is not None else "N/A"

    checks = [
        {"key": "revenue_growth", "name": "売上成長率(YoY)", "value": _pct(rev), "threshold": "≥10%",
         "passed": bool(rev is not None and rev >= 0.10), "available": rev is not None},
        {"key": "earnings_growth", "name": "利益成長率(YoY)", "value": _pct(earn), "threshold": "≥20%",
         "passed": bool(earn is not None and earn >= 0.20), "available": earn is not None},
        {"key": "operating_margin", "name": "営業利益率", "value": _pct(opm), "threshold": "≥10%",
         "passed": bool(opm is not None and opm >= 0.10), "available": opm is not None},
        {"key": "roe", "name": "ROE", "value": _pct(roe), "threshold": "≥12%",
         "passed": bool(roe is not None and roe >= 0.12), "available": roe is not None},
    ]
    available = sum(1 for c in checks if c["available"])
    passed = sum(1 for c in checks if c["passed"])
    total = len(checks)
    score = round(passed / total * 100, 1)
    # 強気ゲートなので厳しめ：取得できた指標の 75% 以上を通過（最低3指標は必要）
    ok = bool(available >= 3 and (passed / available) >= 0.75)
    return {"checks": checks, "passed": passed, "available": available,
            "total": total, "score": score, "ok": ok}


class AggressiveGrowthStrategy(StyleStrategy):
    """DUKE 5章の業績基準（売上+10%・利益+20%・営業利益率10%・ROE12%超）を、年次YoYで
    近似した強気グロース・ゲート。チャート型(breakout_patterns)と AND 結合する想定。
    """
    style_name = "aggressive_growth"
    display_name = "強気グロース（高成長）"
    description = "売上+10%・利益+20%・営業利益率10%・ROE12%超の高成長銘柄（新高値ブレイクの業績基準を年次YoYで近似）"
    needs_fundamentals = True
    category = "fundamental"
    # 「新高値ブレイク投資術」(new_high_breakout) の内部部品（ファンダ側）。一覧からは隠す。
    hidden = True
    filters = [
        FilterDef("revenue_growth", "売上成長率 ≥ 10%", "トップライン高成長", True),
        FilterDef("earnings_growth", "利益成長率 ≥ 20%", "ボトムライン高成長", True),
        FilterDef("operating_margin", "営業利益率 ≥ 10%", "本業の高収益性", True),
        FilterDef("roe", "ROE ≥ 12%", "高い資本効率", True),
    ]

    def evaluate(self, code, name, sector, df, fundamentals=None, enabled_filters=None, near_miss=False):
        if not fundamentals:
            return None
        enabled = self._resolve_enabled(enabled_filters)
        gate = evaluate_growth_gate(fundamentals)
        sigs, signal_keys = [], []
        for c in gate["checks"]:
            sigs.append(Signal(c["name"], c["value"], c["threshold"], c["passed"], "yfinance fundamentals"))
            signal_keys.append(c["key"])
        return self._finalize(code, name, sector, sigs, enabled, signal_keys, df, near_miss=near_miss)


class NewHighBreakoutStrategy(StyleStrategy):
    """DUKE『新高値ブレイク投資術』を、テクニカル(新高値ブレイク)×ファンダ(強気業績)を
    1 つに内包した自己完結の独立した投資法として登録する。

    既存の部品（creeping_breakout / breakout_patterns / aggressive_growth など）とは
    切り離した独立メソッド。本書の核「テクニカルでもファンダでも買い」を、この戦略単体で
    AND 評価する（needs_fundamentals=True）。複数投資法を掛け合わせたいときは、これを
    他スタイルと combine_mode="all" で併用すればよい。
    """
    style_name = "new_high_breakout"
    display_name = "新高値ブレイク（DUKE）"
    description = ("テクニカル(52週高値圏＋カップ/VCP/ボックス＋出来高)とファンダ(売上+10%/利益+20%/"
                  "営業利益率10%/ROE12%)の両方を満たす複合メソッド。単体で『両方で買い』を判定（DUKE）")
    needs_fundamentals = True
    category = "hybrid"
    filters = [
        # --- テクニカル（新高値ブレイク）---
        FilterDef("near_high", "52週高値乖離 ≤ 10%", "高値圏に位置", True),
        FilterDef("above_sma200", "200日MA上抜け", "中長期の上昇トレンド", True),
        FilterDef("chart_pattern", "カップ/VCP/ボックスのいずれか成立", "代表的なブレイク型", True),
        FilterDef("vol_breakout", "出来高 ≥ 20日平均の1.3x", "ブレイク時の出来高増加", True),
        # --- ファンダ（強気業績・5章）---
        FilterDef("revenue_growth", "売上成長率(YoY) ≥ 10%", "トップライン高成長", True),
        FilterDef("earnings_growth", "利益成長率(YoY) ≥ 20%", "ボトムライン高成長", True),
        FilterDef("operating_margin", "営業利益率 ≥ 10%", "本業の高収益性", True),
        FilterDef("roe", "ROE ≥ 12%", "高い資本効率", True),
    ]

    def evaluate(self, code, name, sector, df, fundamentals=None, enabled_filters=None, near_miss=False):
        if df is None or len(df) < 200:
            return None
        if not fundamentals:
            return None
        enabled = self._resolve_enabled(enabled_filters)
        close, high, low, volume = df["Close"], df["High"], df["Low"], df["Volume"]

        # --- テクニカル ---
        is_near, gap, _ = TechnicalSignals.near_52w_high(close, high, tolerance=0.10)
        sma200 = TechnicalSignals.sma(close, 200)
        sma200_val = float(sma200.iloc[-1]) if sma200.iloc[-1] == sma200.iloc[-1] else None
        last_close = float(close.iloc[-1])
        above_sma200 = bool(sma200_val and last_close > sma200_val)
        box = TechnicalSignals.detect_box_breakout(close, high, low, volume)
        vcp = TechnicalSignals.detect_vcp(close, high, low, volume)
        cup = TechnicalSignals.detect_cup_with_handle(close, high, low, volume)
        matched = [lbl for lbl, d in (("ボックス", box), ("VCP", vcp), ("カップ", cup)) if d.get("detected")]
        pattern_ok = len(matched) > 0
        vr = TechnicalSignals.latest_volume_vs_avg(volume, 20)

        # --- ファンダ（強気業績ゲートを共有）---
        gate = evaluate_growth_gate(fundamentals)
        gate_by_key = {c["key"]: c for c in gate["checks"]}

        def _fund_sig(key, label):
            c = gate_by_key.get(key, {})
            return Signal(label, c.get("value", "N/A"), c.get("threshold", ""), bool(c.get("passed")),
                          "yfinance fundamentals")

        sigs = [
            Signal("52週高値乖離 ≤ 10%", f"{gap * 100:.2f}%", "≤10.00%", is_near),
            Signal(
                "200日MA上抜け",
                f"{((last_close - sma200_val) / sma200_val * 100):+.2f}%" if sma200_val else "N/A",
                ">0%", above_sma200,
            ),
            Signal("チャート型成立", "・".join(matched) if matched else "なし", "いずれか成立", pattern_ok),
            Signal("出来高 ≥ 1.3x", f"{vr:.2f}x", "≥1.30x", vr >= 1.30),
            _fund_sig("revenue_growth", "売上成長率(YoY)"),
            _fund_sig("earnings_growth", "利益成長率(YoY)"),
            _fund_sig("operating_margin", "営業利益率"),
            _fund_sig("roe", "ROE"),
        ]
        signal_keys = [
            "near_high", "above_sma200", "chart_pattern", "vol_breakout",
            "revenue_growth", "earnings_growth", "operating_margin", "roe",
        ]
        bonus = 10.0 * len(matched)  # 複数の型が同時成立するほど加点
        return self._finalize(code, name, sector, sigs, enabled, signal_keys, df, score_bonus=bonus, near_miss=near_miss)


def evaluate_excel_stock_gate(fundamentals: Optional[dict]) -> dict:
    """森口亮『1日5分の分析から月13万円を稼ぐExcel株投資』のファンダ基準を決定論的に判定する。

    会社四季報の数字で「成長×収益×割安」を見る同書のウォッチ/本命基準を get_fundamentals
    の値で近似。署名的な指標は『40%ルール』(増収率+営業利益率≥40%)と『PSR』(時価総額÷売上高)。
    四半期QoQ系列が無いため成長率は年次YoYで近似する。
    """
    f = fundamentals or {}
    rev = f.get("revenue_growth")
    earn = f.get("earnings_growth")
    opm = f.get("operating_margin")
    per = f.get("per") or f.get("forward_per")
    mcap = f.get("market_cap_jpy")
    revenue = f.get("revenue")
    psr = (mcap / revenue) if (mcap and revenue and revenue > 0) else None
    # 40%ルール: 増収率(%) + 営業利益率(%)
    forty = ((rev + opm) * 100) if (rev is not None and opm is not None) else None

    def _pct(v):
        return f"{v * 100:.2f}%" if v is not None else "N/A"

    checks = [
        {"key": "revenue_growth", "name": "売上高成長率", "value": _pct(rev), "threshold": "≥10%",
         "passed": bool(rev is not None and rev >= 0.10), "available": rev is not None},
        {"key": "earnings_growth", "name": "利益成長率", "value": _pct(earn), "threshold": "≥10%",
         "passed": bool(earn is not None and earn >= 0.10), "available": earn is not None},
        {"key": "operating_margin", "name": "営業利益率", "value": _pct(opm), "threshold": "≥10%",
         "passed": bool(opm is not None and opm >= 0.10), "available": opm is not None},
        {"key": "forty_rule", "name": "40%ルール(増収率+営業利益率)",
         "value": (f"{forty:.1f}%" if forty is not None else "N/A"), "threshold": "≥40%",
         "passed": bool(forty is not None and forty >= 40.0), "available": forty is not None},
        {"key": "valuation", "name": "PER割安", "value": (f"{per:.2f}" if per else "N/A"),
         "threshold": "0<PER≤25", "passed": bool(per and 0 < per <= 25), "available": per is not None},
        {"key": "psr", "name": "PSR割安", "value": (f"{psr:.2f}倍" if psr is not None else "N/A"),
         "threshold": "≤10倍", "passed": bool(psr is not None and psr <= 10), "available": psr is not None},
    ]
    available = sum(1 for c in checks if c["available"])
    passed = sum(1 for c in checks if c["passed"])
    total = len(checks)
    score = round(passed / total * 100, 1)
    # 取得できた指標の 70% 以上を通過（最低4指標は必要）
    ok = bool(available >= 4 and (passed / available) >= 0.7)
    return {"checks": checks, "passed": passed, "available": available,
            "total": total, "score": score, "ok": ok}


class ExcelStockStrategy(StyleStrategy):
    """森口亮『Excel株投資』の銘柄選定法を独立メソッド化した、決定論的ファンダ投資法。

    会社四季報の業績で「成長(売上・利益10%超)×収益(営業利益率10%超)×割安(PER/PSR)」を見て、
    署名的な『40%ルール』(増収率+営業利益率≥40%)を満たすウォッチ/本命銘柄を抽出する。
    既存の fundamental_gate（村上式）や aggressive_growth とは切り離した独立メソッド。
    """
    style_name = "excel_stock"
    display_name = "Excel株投資（森口亮）"
    description = ("売上・利益10%超成長×営業利益率10%超×割安(PER/PSR)＋40%ルール(増収率+営業利益率≥40%)"
                  "で選ぶ、会社四季報ベースのファンダ投資法（森口亮『Excel株投資』）")
    needs_fundamentals = True
    category = "fundamental"
    filters = [
        FilterDef("revenue_growth", "売上高成長率 ≥ 10%", "トップライン成長", True),
        FilterDef("earnings_growth", "利益成長率 ≥ 10%", "ボトムライン成長", True),
        FilterDef("operating_margin", "営業利益率 ≥ 10%", "本業の収益性", True),
        FilterDef("forty_rule", "40%ルール（増収率＋営業利益率 ≥ 40%）", "成長性＋収益性の合算指標", True),
        FilterDef("valuation", "PER 割安 (0<PER≤25)", "利益面の割安さ", True),
        FilterDef("psr", "PSR ≤ 10倍", "売上面の割安さ（時価総額÷売上高）", True),
    ]

    def evaluate(self, code, name, sector, df, fundamentals=None, enabled_filters=None, near_miss=False):
        if not fundamentals:
            return None
        enabled = self._resolve_enabled(enabled_filters)
        gate = evaluate_excel_stock_gate(fundamentals)
        sigs, signal_keys = [], []
        for c in gate["checks"]:
            sigs.append(Signal(c["name"], c["value"], c["threshold"], c["passed"],
                               "yfinance fundamentals（会社四季報相当）"))
            signal_keys.append(c["key"])
        return self._finalize(code, name, sector, sigs, enabled, signal_keys, df, near_miss=near_miss)


def evaluate_earnings_momentum(df, fundamentals: Optional[dict]) -> dict:
    """決算モメンタム投資（kenmo『5年で1億』PART5）を決定論的に判定する。

    好決算を起点に株価上昇へ勢い（モメンタム）がつき、まだ過熱していない銘柄を拾う。
    yfinance に四半期サプライズが無いため、price の決算ギャップ（窓開け急騰＋出来高急増）を
    好決算サプライズの代理とし、増益（earnings_growth / earnings_quarterly_growth > 0）で
    裏付ける。さらに「ギャップ後に上昇を維持」「短期トレンド良好」「過熱しすぎていない」を見る。

    返り値の checks 各要素: {key, name, value, threshold, passed, available}
    df が無い/短い場合は ok=False。
    """
    if df is None or len(df) < 60:
        return {"checks": [], "passed": 0, "available": 0, "total": 0, "score": 0.0, "ok": False}
    close = df["Close"]
    open_s = df["Open"] if "Open" in df.columns else close
    volume = df["Volume"]
    gap = TechnicalSignals.detect_earnings_gap(open_s, close, volume)

    last_close = float(close.iloc[-1])
    sma25 = TechnicalSignals.sma(close, 25)
    sma25_val = float(sma25.iloc[-1]) if sma25.iloc[-1] == sma25.iloc[-1] else None
    above_sma25 = bool(sma25_val and last_close > sma25_val)
    rsi = TechnicalSignals.rsi(close)
    rsi_val = float(rsi.iloc[-1]) if rsi.iloc[-1] == rsi.iloc[-1] else None

    f = fundamentals or {}
    earn = f.get("earnings_growth")
    earn_q = f.get("earnings_quarterly_growth")
    earn_pos = None
    if earn_q is not None:
        earn_pos = earn_q
    elif earn is not None:
        earn_pos = earn

    detected = bool(gap.get("detected"))
    run_up = gap.get("run_up_pct")
    # 過熱なし: ギャップ起点からの上昇が +18% 以内（ひと相場の初動・まだ伸びしろ）
    not_overheated = bool(detected and run_up is not None and run_up <= 18.0) and \
        (rsi_val is None or rsi_val <= 78)

    checks = [
        {"key": "earnings_gap", "name": "決算ギャップ（窓開け急騰＋出来高）",
         "value": (f"+{gap['gap_pct']}%・{gap['days_ago']}日前・出来高{gap['vol_ratio']}x" if detected else "なし"),
         "threshold": "直近12日内に窓開け+4%超×出来高1.5x", "passed": detected, "available": True},
        {"key": "gap_held", "name": "ギャップ後の上昇維持",
         "value": (f"維持(終値≥ギャップ日終値の97%)" if gap.get("held") else "未維持") if detected else "N/A",
         "threshold": "ギャップ後に終値を維持", "passed": bool(gap.get("held")), "available": detected},
        {"key": "uptrend", "name": "短期トレンド（25日線上）",
         "value": (f"{((last_close - sma25_val) / sma25_val * 100):+.1f}%" if sma25_val else "N/A"),
         "threshold": "25日MA上", "passed": above_sma25, "available": sma25_val is not None},
        {"key": "earnings_positive", "name": "好決算の裏付け（増益）",
         "value": (f"{earn_pos * 100:+.1f}%" if earn_pos is not None else "N/A"),
         "threshold": "増益（>0）", "passed": bool(earn_pos is not None and earn_pos > 0),
         "available": earn_pos is not None},
        {"key": "not_overheated", "name": "過熱なし（初動・伸びしろ）",
         "value": (f"ギャップから+{run_up}%・RSI{rsi_val:.0f}" if (detected and rsi_val is not None) else (f"ギャップから+{run_up}%" if detected else "N/A")),
         "threshold": "ギャップから+18%以内・RSI≤78", "passed": not_overheated, "available": detected},
    ]
    available = sum(1 for c in checks if c["available"])
    passed = sum(1 for c in checks if c["passed"])
    total = len(checks)
    score = round(passed / total * 100, 1)
    # 決算ギャップが大前提。検出された上で取得できた条件の 60% 以上を通過。
    ok = bool(detected and available >= 3 and (passed / available) >= 0.6)
    return {"checks": checks, "passed": passed, "available": available,
            "total": total, "score": score, "ok": ok, "gap": gap}


class EarningsMomentumStrategy(StyleStrategy):
    """決算モメンタム投資（kenmo『5年で1億』PART5）の独立メソッド。

    好決算を起点とした株価上昇の初動（窓開け急騰＝決算ギャップ）を、出来高急増・上昇維持・
    増益・過熱なしで裏取りして拾う順張りイベントメソッド。yfinance に四半期サプライズが
    無いため価格ギャップを代理シグナルにする近似。買い後は出口層（損切り/トレイリング）で
    管理する想定。
    """
    style_name = "earnings_momentum"
    display_name = "決算モメンタム（kenmo）"
    description = ("好決算を起点に上昇へ勢いがついた初動を、決算ギャップ（窓開け急騰＋出来高急増）"
                  "・上昇維持・増益・過熱なしで判定する順張りイベントメソッド（kenmo『5年で1億』）")
    needs_fundamentals = True
    category = "technical"
    filters = [
        FilterDef("earnings_gap", "決算ギャップ（窓開け+4%超×出来高1.5x）", "好決算サプライズの初動", True),
        FilterDef("gap_held", "ギャップ後の上昇維持", "ギャップ日終値を維持している", True),
        FilterDef("uptrend", "25日MA上（短期トレンド）", "上昇トレンドに乗っている", True),
        FilterDef("earnings_positive", "増益（好決算の裏付け）", "利益が伸びている", True),
        FilterDef("not_overheated", "過熱なし（ギャップから+18%以内・RSI≤78）", "まだ伸びしろがある初動", True),
    ]

    def evaluate(self, code, name, sector, df, fundamentals=None, enabled_filters=None, near_miss=False):
        if df is None or len(df) < 60:
            return None
        enabled = self._resolve_enabled(enabled_filters)
        gate = evaluate_earnings_momentum(df, fundamentals)
        if not gate["checks"]:
            return None
        sigs, signal_keys = [], []
        for c in gate["checks"]:
            sigs.append(Signal(c["name"], c["value"], c["threshold"], c["passed"],
                               "yfinance OHLCV + fundamentals"))
            signal_keys.append(c["key"])
        return self._finalize(code, name, sector, sigs, enabled, signal_keys, df, near_miss=near_miss)


class SmallCapGrowthStrategy(StyleStrategy):
    """中長期・小型成長株メソッド（片山『勝つ投資』/kenmo PART6）。

    機関投資家がカバーしづらい中小型（情報の非効率）で、売上の伸びを絶対条件に、
    収益性・資本効率・割安(PSR)が揃った銘柄を中長期目線で拾う。五月の『変化』と
    小型成長の思想を、yfinance のファンダで判定する独立メソッド。真のヒストリカルPER
    （対自分株価の割安度）は単一銘柄診断 analyze_projection 側で併用する。
    """
    style_name = "small_cap_growth"
    display_name = "中長期・小型成長（片山晃/kenmo）"
    description = ("時価総額の小さい中小型株で、売上の伸び（増収）を絶対条件に、収益性・ROE・"
                  "割安(PSR)が揃った銘柄を中長期で拾う（片山『勝つ投資』/kenmo 中長期投資）")
    needs_fundamentals = True
    category = "fundamental"
    filters = [
        FilterDef("small_cap", "時価総額 ≤ 1000億円", "中小型株（情報の非効率に妙味）", True),
        FilterDef("revenue_growth", "売上成長率 ≥ 10%", "売上の伸び（中長期の絶対条件）", True),
        FilterDef("operating_margin", "営業利益率 ≥ 8%", "本業の収益性", True),
        FilterDef("roe", "ROE ≥ 10%", "資本効率が高い", True),
        FilterDef("psr", "PSR ≤ 10倍", "売上面で割高すぎない（暴騰後を避ける）", True),
    ]

    def evaluate(self, code, name, sector, df, fundamentals=None, enabled_filters=None, near_miss=False):
        if not fundamentals:
            return None
        enabled = self._resolve_enabled(enabled_filters)
        mcap = fundamentals.get("market_cap_jpy")
        rev = fundamentals.get("revenue_growth")
        opm = fundamentals.get("operating_margin")
        roe = fundamentals.get("roe")
        revenue = fundamentals.get("revenue")
        psr = (mcap / revenue) if (mcap and revenue and revenue > 0) else None
        cap_oku = (mcap / 1e8) if mcap else None  # 円→億円

        sigs = [
            Signal("時価総額", f"{cap_oku:,.0f}億円" if cap_oku else "N/A", "≤1000億円",
                   bool(mcap and mcap <= 1e11), "yfinance fundamentals"),
            Signal("売上成長率", f"{rev * 100:.2f}%" if rev is not None else "N/A", "≥10%",
                   bool(rev is not None and rev >= 0.10), "yfinance fundamentals"),
            Signal("営業利益率", f"{opm * 100:.2f}%" if opm is not None else "N/A", "≥8%",
                   bool(opm is not None and opm >= 0.08), "yfinance fundamentals"),
            Signal("ROE", f"{roe * 100:.2f}%" if roe is not None else "N/A", "≥10%",
                   bool(roe is not None and roe >= 0.10), "yfinance fundamentals"),
            Signal("PSR", f"{psr:.2f}倍" if psr is not None else "N/A", "≤10倍",
                   bool(psr is not None and psr <= 10), "yfinance fundamentals"),
        ]
        signal_keys = ["small_cap", "revenue_growth", "operating_margin", "roe", "psr"]
        return self._finalize(code, name, sector, sigs, enabled, signal_keys, df, near_miss=near_miss)


class AssetValueStrategy(StyleStrategy):
    """資産バリュー株メソッド（たーちゃん『50万円を50億円に』PART2）。

    「資産」に対して株価が激安な銘柄を拾う。古い簿価のまま放置された土地・有価証券の
    含み益が、TOB/MBO/アクティビスト・特別配当などをきっかけに顕在化して株価が上がる、
    という資産価値ベースの順当な割安投資。真の含み資産（簿価vs時価）は yfinance に無いため、
    PBR≤0.5（純資産の半値以下）を核に、黒字（利益面でも割高でない）とインカム下支えで近似する。
    """
    style_name = "asset_value"
    display_name = "資産バリュー（たーちゃん）"
    description = ("PBRが極端に低く（純資産の半値以下）、利益面でも割高でなく配当で下支えされた、"
                  "資産価値に対して激安な銘柄を拾う（たーちゃん『50万円を50億円に』資産バリュー）")
    needs_fundamentals = True
    category = "fundamental"
    filters = [
        FilterDef("pbr", "PBR ≤ 0.5", "純資産の半値以下＝資産に対して激安", True),
        FilterDef("per", "PER ≤ 15", "赤字垂れ流しでなく利益面でも割安", True),
        FilterDef("dividend", "配当利回り ≥ 2%", "含み資産が動くまでのインカム下支え", True),
    ]

    def evaluate(self, code, name, sector, df, fundamentals=None, enabled_filters=None, near_miss=False):
        if not fundamentals:
            return None
        enabled = self._resolve_enabled(enabled_filters)
        pbr = fundamentals.get("pbr")
        per = fundamentals.get("per")
        div_yield = fundamentals.get("dividend_yield")
        if div_yield is not None and div_yield > 1:
            div_yield = div_yield / 100.0

        sigs = [
            Signal("PBR", f"{pbr:.2f}" if pbr else "N/A", "≤0.5",
                   bool(pbr and 0 < pbr <= 0.5), "yfinance fundamentals"),
            Signal("PER", f"{per:.2f}" if per else "N/A", "≤15",
                   bool(per and 0 < per <= 15), "yfinance fundamentals"),
            Signal("配当利回り", f"{div_yield * 100:.2f}%" if div_yield is not None else "N/A", "≥2%",
                   bool(div_yield is not None and div_yield >= 0.02), "yfinance fundamentals"),
        ]
        signal_keys = ["pbr", "per", "dividend"]
        return self._finalize(code, name, sector, sigs, enabled, signal_keys, df, near_miss=near_miss)


# 景気循環（シクリカル）セクター。景気の波で業績・株価が大きく振れる業種（東証33業種の名称で部分一致）。
_CYCLICAL_SECTORS = ("鉄鋼", "非鉄金属", "海運", "ガラス・土石", "石油・石炭", "化学",
                     "繊維", "パルプ・紙", "機械", "電気機器", "輸送用機器", "ゴム製品", "金属製品")


class CyclicalValueStrategy(StyleStrategy):
    """シクリカルバリュー株メソッド（たーちゃん『50万円を50億円に』PART4）。

    鉄鋼・海運・半導体・化学などの景気循環業種で、いま「景気の谷」にあり採算が悪化して
    （低営業利益率・赤字含む）売上に対して激安（低PSR）な銘柄を、谷からの反転の初動で拾う。
    赤字→黒字転換でインパクトが大きい。増益を絶対条件にする他メソッドと真逆の発想なので、
    PERではなくPSRと景気フェーズで判定する独立メソッド。景気は約4年で循環する。
    """
    style_name = "cyclical_value"
    display_name = "シクリカルバリュー（たーちゃん）"
    description = ("景気循環業種で、いま採算が悪化し（低営業利益率）売上に対して激安（低PSR）な銘柄を、"
                  "谷からの反転初動で拾う。赤字→黒字転換を狙う（たーちゃん『50万円を50億円に』シクリカル）")
    needs_fundamentals = True
    category = "hybrid"
    filters = [
        FilterDef("cyclical_sector", "景気循環セクター", "鉄鋼/海運/半導体/化学など景気の波で振れる業種", True),
        FilterDef("trough_margin", "営業利益率 ≤ 5%", "景気の谷＝採算悪化（赤字含む）", True),
        FilterDef("low_psr", "PSR ≤ 1倍", "売上に対して激安＝下値限定", True),
        FilterDef("turnaround", "60日MA上（反転の初動）", "谷からの反転が始まっている", True),
    ]

    def evaluate(self, code, name, sector, df, fundamentals=None, enabled_filters=None, near_miss=False):
        if not fundamentals:
            return None
        enabled = self._resolve_enabled(enabled_filters)
        mcap = fundamentals.get("market_cap_jpy")
        revenue = fundamentals.get("revenue")
        opm = fundamentals.get("operating_margin")
        psr = (mcap / revenue) if (mcap and revenue and revenue > 0) else None
        sec_txt = sector or fundamentals.get("sector") or ""
        is_cyclical = any(k in sec_txt for k in _CYCLICAL_SECTORS)

        ma_up = False
        ma_val = None
        if df is not None and len(df) >= 60:
            close = df["Close"].astype(float)
            try:
                ma_val = float(TechnicalSignals.sma(close, 60).iloc[-1])
                last = float(close.iloc[-1])
                ma_up = bool(ma_val and last > ma_val)
            except Exception:
                ma_up = False

        sigs = [
            Signal("セクター", sec_txt or "N/A", "景気循環業種", bool(is_cyclical), "universe/yfinance"),
            Signal("営業利益率", f"{opm * 100:.2f}%" if opm is not None else "N/A", "≤5%（谷）",
                   bool(opm is not None and opm <= 0.05), "yfinance fundamentals"),
            Signal("PSR", f"{psr:.2f}倍" if psr is not None else "N/A", "≤1倍",
                   bool(psr is not None and psr <= 1.0), "yfinance fundamentals"),
            Signal("60日MA", f"{ma_val:,.0f}円上" if (ma_up and ma_val) else ("MA下" if ma_val else "N/A"), "MA上で反転初動",
                   bool(ma_up), "yfinance OHLCV"),
        ]
        signal_keys = ["cyclical_sector", "trough_margin", "low_psr", "turnaround"]
        return self._finalize(code, name, sector, sigs, enabled, signal_keys, df, near_miss=near_miss)


STRATEGY_REGISTRY: dict[str, StyleStrategy] = {
    s.style_name: s for s in [
        NewHighBreakoutStrategy(),    # ← DUKE『新高値ブレイク投資術』の独立メソッド
        ExcelStockStrategy(),         # ← 森口『Excel株投資』の独立メソッド
        EarningsMomentumStrategy(),   # ← kenmo『5年で1億』決算モメンタムの独立メソッド
        SmallCapGrowthStrategy(),     # ← 片山『勝つ投資』/kenmo 中長期・小型成長の独立メソッド
        AssetValueStrategy(),         # ← たーちゃん『50万円を50億円に』資産バリューの独立メソッド
        CyclicalValueStrategy(),      # ← たーちゃん『50万円を50億円に』シクリカルバリューの独立メソッド
        CreepingBreakoutStrategy(),
        BreakoutPatternStrategy(),    # 部品(hidden): 新高値ブレイクのテクニカル単体
        AggressiveGrowthStrategy(),   # 部品(hidden): 強気業績ゲート単体
        FundamentalGateStrategy(),
    ]
}


def get_strategy(name: str) -> Optional[StyleStrategy]:
    return STRATEGY_REGISTRY.get(name)


# =========================================================
# 2層モデルの可視化：共通ファクター軸（下層）とメソッド→軸の地図
# =========================================================
# 各メソッド（上層）は、下の共通ファクター軸（下層ゲート）の組み合わせとして表せる。
# これを明示することで「どのメソッドがどの軸をカバーし、どこが重複か」「掛け合わせ＝
# 軸の和集合」が一目で分かる。整理（方向性）の地図。
FACTOR_AXES: dict[str, str] = {
    "growth": "成長性（売上・利益の伸び）",
    "quality": "収益性（営業利益率・ROE）",
    "value_earnings": "割安・利益面（PER・PSR）",
    "value_asset": "割安・資産面（PBR・自己資本・配当）",
    "safety": "財務安全性（自己資本比率・営業CF・FCF）",
    "trend": "トレンド（52週高値圏・移動平均）",
    "pattern": "チャート型（カップ/VCP/ボックス）",
    "small_cap": "小型（時価総額の小ささ＝情報の非効率）",
    "event": "イベント（決算サプライズ・モメンタム）",
    "cyclical": "景気循環（谷で買い・黒字転換）",
    "catalyst": "カタリスト（大株主買い増し・物言う株主・TOB/MBO期待）",
}

# メソッド（style_name）→ 構成ファクター軸。これが「メソッド＝軸の組み合わせ」の単一の地図。
STRATEGY_AXES: dict[str, list[str]] = {
    "new_high_breakout": ["trend", "pattern", "growth", "quality"],
    "excel_stock": ["growth", "quality", "value_earnings"],
    "earnings_momentum": ["event", "trend"],
    "small_cap_growth": ["small_cap", "growth", "quality", "value_earnings"],
    "asset_value": ["value_asset", "safety"],
    "cyclical_value": ["cyclical", "value_earnings", "trend"],
    "creeping_breakout": ["trend"],
    "breakout_patterns": ["trend", "pattern"],
    "aggressive_growth": ["growth", "quality"],
    "fundamental_gate": ["growth", "quality", "value_earnings"],
}


def factor_axes_catalog() -> list[dict]:
    """共通ファクター軸（下層ゲートの語彙）の一覧を返す。"""
    return [{"key": k, "label": v} for k, v in FACTOR_AXES.items()]


def list_strategies() -> list[dict]:
    return [
        {
            "name": s.style_name,
            "display_name": s.display_name,
            "description": s.description,
            "category": s.category,
            "needs_fundamentals": s.needs_fundamentals,
            "axes": STRATEGY_AXES.get(s.style_name, []),
            "axis_labels": [FACTOR_AXES[a] for a in STRATEGY_AXES.get(s.style_name, []) if a in FACTOR_AXES],
            "filters": s.list_filters(),
        }
        for s in STRATEGY_REGISTRY.values()
        if not getattr(s, "hidden", False)
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
            notes.append(f"現レッグは起点から約{current_gain * 100:.0f}%上昇で、過去中央値（{(g50 or 0) * 100:.0f}%）を超過＝強いトレンドのサイン。"
                         "本当に上がる銘柄は過去の範囲を超えて伸びるので、ここで降りず利を伸ばす局面。下のトレイリングストップで下落転換に備える。")
        else:
            notes.append(f"現レッグは起点から約{current_gain * 100:.0f}%上昇。過去中央値まで概算で残り約{remaining_pct:.0f}%の余地。")

    # 総合判定（ざっくり）。
    # 方針：過去の値動きを「上限」として天井を決めつけない。大きく上がる銘柄ほど過去の
    # 典型を超えて伸びるので、伸びている＝強いトレンドと捉え、利確はトレイリングストップ
    # 割れ（＝下落転換の兆し）で行う。新規エントリーの可否だけは損切り幅(R/R)で慎重に見る。
    # entry_caution: 新規で飛び乗るには損切り幅が広く R/R が悪い（保有継続の妨げにはしない）。
    entry_caution = bool(rr is not None and rr < 1)
    if current_gain >= (g75 or 1e9):
        verdict = ("強いトレンド：過去ブレイクの上位水準を超えて上昇中。天井を決めつけず、"
                   "トレイリングストップ割れまで保有して利を伸ばす（下落転換の兆しが出たら利確）。")
    elif rr is not None and rr >= 2:
        verdict = "妙味あり：リスクリワード良好で、過去の典型的な上昇余地もまだ残る。トレンドに沿って利を伸ばす。"
    elif entry_caution:
        verdict = ("新規は慎重：直近高値まで近く損切り幅に対する当面の利幅が小さい。"
                   "打診的に小さく入るか、押し目・再ブレイクを待つ（既保有なら継続でよい）。")
    else:
        verdict = "順張り継続：トレンドに沿って保有し利を伸ばす。トレイリングストップ（下記）割れで利確。"

    # --- 5分割法 + -10%損切り（DUKE 6章の資金管理）。出口層の単一ソースに委譲 ---
    buy_plan = build_tranche_plan(last_close, hard_stop_pct=-0.10)

    return {
        "ok": True,
        "as_of": as_of,
        "last_close": round(last_close, 1) if last_close else None,
        "atr": round(last_atr, 2) if last_atr else None,
        "buy_plan": buy_plan,
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
        "entry_caution": entry_caution,
        "notes": notes,
    }


def estimate_target_price_by_multiple(
    fundamentals: Optional[dict], last_close: Optional[float],
    *, years: int = 3, growth_cap: float = 0.40,
) -> dict:
    """営業利益倍率（時価総額 ÷ 営業利益）を使った目標株価の概算（DUKE 7章の類似会社比較法の簡易版）。

    現在の営業利益倍率を維持すると仮定し、years 年後の営業利益（利益成長率で複利）に同倍率を
    掛けて将来時価総額→1株あたりに割り戻す。yfinance に四半期/中計が無いため、年次の
    earnings_growth を営業利益成長の代理に使う（暴走を避けるため growth_cap で上限）。
    """
    f = fundamentals or {}
    mcap = f.get("market_cap_jpy")
    rev = f.get("revenue")
    opm = f.get("operating_margin")
    shares = f.get("shares_outstanding")
    growth = f.get("earnings_growth")
    if not (mcap and rev and opm and shares and last_close and growth is not None):
        return {"ok": False, "reason": "営業利益倍率の算出に必要なデータ（時価総額/売上/営業利益率/株数/成長率）が不足"}
    op_income = rev * opm
    if op_income <= 0:
        return {"ok": False, "reason": "営業利益がゼロ以下のため倍率法は適用外"}
    op_multiple = mcap / op_income
    g = max(-0.5, min(float(growth), growth_cap))  # 過度な成長率を抑制
    future_op = op_income * ((1 + g) ** years)
    targets = {}
    for label, mult in (("保守", op_multiple * 0.8), ("本命", op_multiple)):
        tgt_price = future_op * mult / shares
        targets[label] = {
            "price": round(tgt_price, 1),
            "upside_pct": round((tgt_price - last_close) / last_close * 100, 1),
        }
    return {
        "ok": True,
        "op_multiple": round(op_multiple, 1),
        "assumed_growth_pct": round(g * 100, 1),
        "years": years,
        "targets": targets,
        "note": (f"現在の営業利益倍率 約{op_multiple:.1f}倍を基準に、利益成長 {g * 100:.0f}%/年で"
                 f"{years}年後営業利益を複利推定（年次YoYを営業利益成長の代理に使用）。"),
    }


def evaluate_historical_per(per_history: Optional[list], current_per: Optional[float]) -> dict:
    """ヒストリカルPER（片山『勝つ投資』/kenmo）。一律のPER水準ではなく、その銘柄自身の
    過去PERレンジの中で現在のPERが割安か割高かを「対自分株価」で評価する。

    per_history: [{"year": 2021, "per": 18.2}, ...]（正のPERのみ有効）。
    過去分布の下位なら割安・上位なら割高。3期以上の有効PERが必要。決定論的。
    """
    import numpy as np
    pers = [float(h["per"]) for h in (per_history or [])
            if h.get("per") is not None and h.get("per") > 0]
    if current_per is None or current_per <= 0 or len(pers) < 3:
        return {"ok": False, "reason": "有効なヒストリカルPERが3期以上必要"}
    lo, hi, med = float(min(pers)), float(max(pers)), float(np.median(pers))
    # 現在PERが過去分布の何%地点か（小さいほど割安）
    pctl = round(sum(1 for p in pers if p <= current_per) / len(pers) * 100)
    band = round((current_per - lo) / (hi - lo), 2) if hi > lo else 0.5
    if current_per <= med * 0.9:
        verdict, label = "cheap", "割安（自分の過去比）"
    elif current_per >= med * 1.15:
        verdict, label = "rich", "割高（自分の過去比）"
    else:
        verdict, label = "fair", "中立（過去レンジ内）"
    return {
        "ok": True,
        "current_per": round(current_per, 1),
        "min": round(lo, 1), "median": round(med, 1), "max": round(hi, 1),
        "percentile": pctl, "band_pos": band,
        "verdict": verdict, "verdict_label": label,
        "samples": len(pers),
        "note": (f"過去{len(pers)}期のPERレンジ {lo:.0f}〜{hi:.0f}倍（中央{med:.0f}倍）に対し"
                 f"現在 約{current_per:.0f}倍＝下位{pctl}%。"
                 + ("自分の過去比で割安圏。" if verdict == "cheap"
                    else ("自分の過去比で割高圏（業績の伸びが伴うか要確認）。" if verdict == "rich"
                          else "過去レンジの中ほど。"))),
    }


def evaluate_catalyst(holdings: Optional[dict]) -> dict:
    """カタリスト（木原直哉/エミン『確率思考』）。EDINET 大量保有報告書のサマリーから、
    「大株主の買い増し・物言う株主の登場・複数の大量保有・高い保有割合」を決定論的に点数化する。

    holdings: services.edinet_large_holdings.get_large_holdings_for_code() の返り値。
    軸=catalyst。バッチではなく単一銘柄 deep-dive 層で使う（EDINET 走査が重いため）。
    """
    if not holdings or not holdings.get("ok"):
        return {"ok": False, "reason": (holdings or {}).get("reason") or "大量保有報告書なし"}
    activist = bool(holdings.get("activist_present"))
    accumulating = bool(holdings.get("accumulating"))
    count = int(holdings.get("count") or 0)
    ratio = holdings.get("latest_ratio")
    signals = [
        {"key": "activist", "label": "物言う株主が保有", "hit": activist,
         "weight": 40},
        {"key": "accumulating", "label": "大株主が買い増し", "hit": accumulating,
         "weight": 30},
        {"key": "multiple", "label": "複数の大量保有報告（注目度）", "hit": count >= 2,
         "weight": 15},
        {"key": "high_ratio", "label": "保有割合が高い（≥10%）", "hit": bool(ratio and ratio >= 0.10),
         "weight": 15},
    ]
    score = sum(s["weight"] for s in signals if s["hit"])
    if score >= 55:
        verdict, label = "strong", "強いカタリスト"
    elif score >= 25:
        verdict, label = "mild", "カタリスト候補"
    else:
        verdict, label = "weak", "目立ったカタリストなし"
    return {
        "ok": True,
        "catalyst_score": score,
        "verdict": verdict, "verdict_label": label,
        "activist_present": activist, "accumulating": accumulating,
        "count": count, "latest_ratio": ratio,
        "latest_holder": holdings.get("latest_holder"),
        "holders": holdings.get("holders", []),
        "signals": signals,
        "note": holdings.get("note", ""),
    }


# =========================================================
# 目標配分レイヤー：最高値型:待ち型=4:1／日本株:米国株=1:1（目安表示＋ドリフト警告）
# =========================================================
def classify_portfolio_bucket(res: Optional[dict]) -> str:
    """銘柄を「最高値型(momentum)」か「待ち型(wait)」に分類する。決定論的・純粋関数。

    高値追い（パーフェクトオーダー or 52週高値から5%以内）＝momentum、それ以外＝wait。
    値動きの役割で分けるユーザーの区分（最高値更新に乗る／動かず待ち）に忠実。
    """
    tr = (res or {}).get("trend") or {}
    if tr.get("perfect_order"):
        return "momentum"
    gap = tr.get("gap_to_52w_pct")
    if gap is not None and gap >= -5.0:  # 52週高値から5%以内＝高値追い
        return "momentum"
    return "wait"


def build_allocation_plan(
    positions: Optional[list],
    *,
    momentum_ratio: float = 4.0, wait_ratio: float = 1.0,
    jp_ratio: float = 1.0, us_ratio: float = 1.0,
) -> dict:
    """保有ポジション（共通通貨の時価）から、目標配分に対する現状・ドリフトを出す。純粋関数。

    positions: [{"value": float(共通通貨), "bucket": "momentum"|"wait", "market": "JP"|"US"}]
    目標は 最高値型:待ち型=momentum_ratio:wait_ratio、日本株:米国株=jp_ratio:us_ratio。
    強制リバランスはせず「目安表示＋ドリフト警告」（ソフト誘導）。
    """
    pos = [p for p in (positions or []) if p.get("value") and p["value"] > 0]
    total = sum(p["value"] for p in pos)
    if total <= 0:
        return {"ok": False, "reason": "時価評価できる保有がありません"}

    def _sum(pred):
        return sum(p["value"] for p in pos if pred(p))

    mom = _sum(lambda p: p.get("bucket") == "momentum")
    jp = _sum(lambda p: p.get("market") == "JP")

    def _axis(label, a_key, a_val, a_name, b_name, a_target):
        a_pct, b_pct = a_val / total * 100, (total - a_val) / total * 100
        drift = round(a_pct - a_target * 100, 1)  # a が目標比 何ptオーバー(+)/不足(-)
        over_a = drift > 0
        return {
            "axis": label, "a_key": a_key,
            "a": {"name": a_name, "value": round(a_val), "pct": round(a_pct, 1),
                  "target_pct": round(a_target * 100, 1)},
            "b": {"name": b_name, "value": round(total - a_val), "pct": round(b_pct, 1),
                  "target_pct": round((1 - a_target) * 100, 1)},
            "drift_pct": drift,
            "rebalance_value": abs(round(total * a_target - a_val)),
            "over": a_name if over_a else b_name,
            "under": b_name if over_a else a_name,
            "over_key": (a_key if over_a else ("wait" if a_key == "momentum" else
                                               ("US" if a_key == "JP" else a_key))),
        }

    bucket_axis = _axis("最高値型:待ち型", "momentum", mom, "最高値型", "待ち型",
                        momentum_ratio / (momentum_ratio + wait_ratio))
    market_axis = _axis("日本株:米国株", "JP", jp, "日本株", "米国株",
                        jp_ratio / (jp_ratio + us_ratio))
    warnings = []
    for ax in (bucket_axis, market_axis):
        if abs(ax["drift_pct"]) >= 10:
            warnings.append(f"{ax['axis']}が目標から {ax['drift_pct']:+.0f}pt ズレ"
                            f"（{ax['over']}が過多・{ax['under']}が不足）")
    note = (f"時価合計 約{round(total):,}。"
            + ("／".join(warnings) if warnings
               else "目標（最高値型:待ち型=4:1・日本株:米国株=1:1）におおむね整合。"))
    return {
        "ok": True, "total_value": round(total),
        "bucket_axis": bucket_axis, "market_axis": market_axis,
        "warnings": warnings, "note": note,
        "target": {"momentum": f"{momentum_ratio:.0f}:{wait_ratio:.0f}",
                   "market": f"{jp_ratio:.0f}:{us_ratio:.0f}"},
    }


# =========================================================
# プロセス改善：摩擦(税/手数料)・的中率→建玉・地合い・流動性・買い増し・シグナル検証
# =========================================================
def compute_rotation_friction(shares, last_close, avg_cost, *, account: str = "taxable",
                              tax_rate: float = 0.20315, cost_rate: float = 0.002) -> dict:
    """入替の摩擦（譲渡益課税＋売買コスト往復）を見積もる。決定論的・純粋関数。
    勝ち株を売って入替えると税で目減りする——を可視化し、入替の足切りに使う。
    avg_cost 不明なら税は0（取得単価不明）。cost_rate は売り＋買いの往復概算。
    account: "nisa"/"非課税" は譲渡益非課税で税0。それ以外（特定/一般）は一律 tax_rate。"""
    try:
        sh, lc = float(shares or 0), float(last_close or 0)
    except (TypeError, ValueError):
        return {"ok": False}
    if sh <= 0 or lc <= 0:
        return {"ok": False}
    tax_free = str(account or "").lower() in ("nisa", "非課税", "tax_free")
    position_value = sh * lc
    gain = (lc - float(avg_cost)) * sh if avg_cost else 0.0
    tax = (tax_rate * gain) if (gain > 0 and not tax_free) else 0.0
    cost = cost_rate * position_value
    friction = tax + cost
    return {
        "ok": True, "account": ("nisa" if tax_free else "taxable"),
        "position_value": round(position_value), "gain": round(gain),
        "tax": round(tax), "cost": round(cost), "friction": round(friction),
        "friction_pct": round(friction / position_value * 100, 2) if position_value else 0.0,
        "net_proceeds": round(position_value - friction),
    }


def hit_rate_risk_multiplier(hit_rate, samples, *, min_samples: int = 10) -> float:
    """事後検証の的中率から建玉（リスク）倍率を出す。学習ループを実際の建玉に反映する。
    サンプル不足は中立1.0。60%以上→1.3、40%以下→0.5、間は線形。決定論的。"""
    if hit_rate is None or samples is None or samples < min_samples:
        return 1.0
    if hit_rate >= 60:
        m = 1.3
    elif hit_rate <= 40:
        m = 0.5
    else:
        m = 0.5 + (hit_rate - 40) / 20.0 * 0.8
    return round(max(0.5, min(1.3, m)), 2)


def assess_market_regime(index_df, *, slow: int = 200, fast: int = 50) -> dict:
    """指数(N225/GSPC)から地合いレジームを判定。200日線の上下＋傾きで risk_on/neutral/risk_off。
    『上昇相場でのみ攻める』ための地合いフィルタ。決定論的。"""
    if index_df is None or len(index_df) < slow + 25 or "Close" not in index_df:
        return {"ok": False, "regime": "neutral", "label": "中立（指数データ不足）",
                "note": "指数データ不足で地合い不明（中立扱い）。"}
    close = index_df["Close"]
    last = float(close.iloc[-1])
    ma_s_series = close.rolling(slow).mean()
    ma_s = float(ma_s_series.iloc[-1])
    ma_s_prev = float(ma_s_series.iloc[-21])  # 約1ヶ月前
    above = last > ma_s
    slope_up = ma_s > ma_s_prev
    if above and slope_up:
        regime, label = "risk_on", "リスクオン（上昇基調）"
    elif (not above) and (not slope_up):
        regime, label = "risk_off", "リスクオフ（下落基調）"
    else:
        regime, label = "neutral", "中立（方向感に乏しい）"
    return {
        "ok": True, "regime": regime, "label": label,
        "last": round(last, 1), "ma200": round(ma_s, 1),
        "above_200ma": above, "ma200_slope_up": slope_up,
        "note": (f"指数 {last:.0f}／200日線 {ma_s:.0f}（{'上' if above else '下'}・傾き{'↑' if slope_up else '↓'}）＝{label}。"
                 + ("新規買いは積極可。" if regime == "risk_on"
                    else ("新規買い・入替は抑制し現金比率を上げる局面。" if regime == "risk_off"
                          else "新規買いは厳選。"))),
    }


def assess_liquidity(df, market: str = "JP", *, days: int = 20) -> dict:
    """直近の平均売買代金（出来高×終値）から流動性を評価。薄商いは約定困難＝入替枚数を制限する。
    thin 目安: JP 1億円/日未満・US 100万ドル/日未満。max_buyable=日次代金の10%。決定論的。"""
    if df is None or len(df) < 5 or "Volume" not in df or "Close" not in df:
        return {"ok": False}
    n = min(days, len(df))
    vol = df["Volume"].iloc[-n:].astype(float)
    close = df["Close"].iloc[-n:].astype(float)
    turnover = float((vol * close).mean())
    floor = 1e8 if market == "JP" else 1e6
    return {
        "ok": True, "avg_turnover": round(turnover), "thin": turnover < floor,
        "max_buyable_value": round(turnover * 0.10),
        "unit": "JPY" if market == "JP" else "USD",
    }


def build_pyramid_plan(last_close, avg_cost, atr, *, swing_high=None, add_ratio: float = 0.5) -> dict:
    """勝ち株への買い増し（ピラミッディング）。含み益が乗りトレンド継続中の保有に、直近高値ブレイクで
    控えめに買い増し、損切りを建値付近へ引き上げて『勝ちを伸ばし守る』。決定論的。"""
    if not last_close or not avg_cost or last_close <= avg_cost:
        return {"ok": False, "reason": "含み益が乗っていない（買い増し非推奨）"}
    trigger = round(swing_high, 1) if swing_high else round(last_close, 1)
    new_stop = round(max(float(avg_cost), last_close - 2.0 * atr) if atr else float(avg_cost), 1)
    return {
        "ok": True, "add_ratio_pct": round(add_ratio * 100), "trigger": trigger,
        "raised_stop": new_stop,
        "note": (f"含み益が乗りトレンド継続中。{trigger} の直近高値ブレイクで元玉の"
                 f"{round(add_ratio * 100)}%を買い増し、損切りを建値 {new_stop} 付近へ引き上げ（勝ちを守る）。"),
    }


def backtest_entry_signal(df, *, signal: str = "new_high", lookback: int = 60,
                          horizons=(20, 60)) -> dict:
    """単一銘柄で『エントリーシグナル発生→その後の前向きリターン』を過去全体で集計する簡易バックテスト。
    signal: "new_high"(lookback日高値更新) / "perfect_order"(25>75>200 かつ上向き)。
    各シグナル日からの horizon 営業日後リターンを、同銘柄の全期間平均（buy&hold相当）と比較。
    『高スコアへ入替が買い持ちに勝つか』を銘柄単位で検証する第一歩。決定論的。"""
    import numpy as np
    if df is None or len(df) < lookback + max(horizons) + 5 or "Close" not in df:
        return {"ok": False, "reason": "バックテストに十分な履歴がありません"}
    close = df["Close"].astype(float).reset_index(drop=True)
    n = len(close)
    if signal == "perfect_order":
        sma25 = close.rolling(25).mean()
        sma75 = close.rolling(75).mean()
        sma200 = close.rolling(200).mean()
        sig = (sma25 > sma75) & (sma75 > sma200) & (sma25 > sma25.shift(5))
    else:  # new_high
        roll_high = close.rolling(lookback).max()
        sig = close >= roll_high

    results = {}
    base_all = {}
    for h in horizons:
        fwd = close.shift(-h) / close - 1.0  # 全日からの h 日後リターン（buy&hold基準）
        base_all[h] = fwd.iloc[:n - h]
        idx = [i for i in range(lookback, n - h) if bool(sig.iloc[i])]
        rets = [float(close.iloc[i + h] / close.iloc[i] - 1.0) for i in idx]
        if rets:
            arr = np.array(rets)
            base = float(base_all[h].mean()) if len(base_all[h]) else 0.0
            results[f"d{h}"] = {
                "samples": len(rets),
                "win_rate": round(float((arr > 0).mean()) * 100, 1),
                "avg_return_pct": round(float(arr.mean()) * 100, 1),
                "median_return_pct": round(float(np.median(arr)) * 100, 1),
                "baseline_avg_pct": round(base * 100, 1),       # 全日平均（buy&hold相当）
                "edge_pct": round((float(arr.mean()) - base) * 100, 1),  # シグナルの優位性
            }
        else:
            results[f"d{h}"] = {"samples": 0}
    sig_label = "新高値更新" if signal == "new_high" else "パーフェクトオーダー"
    return {"ok": True, "signal": signal, "signal_label": sig_label, "horizons": list(horizons),
            "results": results}


def backtest_portfolio_rotation(price_df, *, rebalance_days: int = 20, top_k: int = 5,
                                lookback: int = 60, cost_rate: float = 0.002) -> dict:
    """複数銘柄の終値パネル（price_df: index=日付, columns=銘柄コード）で、
    『定期リバランスでモメンタム上位 top_k を等加重保有』する回転戦略を、同じ銘柄群の
    等加重 buy&hold と比較する決定論的バックテスト。各リバランス日までのデータでのみランク
    （先読みなし）し、回転にはコストを課す。『毎日入れ替えが買い持ちに勝つか』のポート単位の検証。"""
    import numpy as np
    import pandas as pd  # type: ignore
    if price_df is None or getattr(price_df, "empty", True):
        return {"ok": False, "reason": "価格パネルが空"}
    df = price_df.dropna(axis=1, how="all").ffill()
    n, m = df.shape
    if n < lookback + rebalance_days + 5 or m < max(2, top_k):
        return {"ok": False, "reason": "バックテストに十分な銘柄/履歴がありません"}
    closes = df.values.astype(float)
    reb_idx = list(range(lookback, n - 1, rebalance_days))
    strat_eq, bh_eq = [1.0], [1.0]
    period_rets, turnovers = [], []
    prev_sel: set = set()
    for i in reb_idx:
        end = min(i + rebalance_days, n - 1)
        base, now = closes[i - lookback], closes[i]
        mom = [(c, now[c] / base[c] - 1.0) for c in range(m)
               if np.isfinite(base[c]) and np.isfinite(now[c]) and base[c] > 0 and np.isfinite(closes[end][c])]
        if not mom:
            continue
        mom.sort(key=lambda x: x[1], reverse=True)
        sel = [c for c, _ in mom[:top_k]]
        pr = [closes[end][c] / closes[i][c] - 1.0 for c in sel if closes[i][c] > 0]
        if not pr:
            continue
        sel_set = set(sel)
        turnover = (len(sel_set ^ prev_sel) / max(1, len(sel_set | prev_sel))) if (prev_sel or sel_set) else 1.0
        strat_ret = float(np.mean(pr)) - cost_rate * turnover
        bh = [closes[end][c] / closes[i][c] - 1.0 for c, _ in mom if closes[i][c] > 0]
        bh_ret = float(np.mean(bh)) if bh else 0.0
        strat_eq.append(strat_eq[-1] * (1 + strat_ret))
        bh_eq.append(bh_eq[-1] * (1 + bh_ret))
        period_rets.append(strat_ret)
        turnovers.append(turnover)
        prev_sel = sel_set
    if len(strat_eq) < 3:
        return {"ok": False, "reason": "リバランス回数が不足"}

    def _max_dd(eq):
        peak, dd = eq[0], 0.0
        for v in eq:
            peak = max(peak, v)
            dd = min(dd, v / peak - 1.0)
        return dd

    years = max(0.1, n / 252.0)
    strat_total, bh_total = strat_eq[-1] - 1, bh_eq[-1] - 1
    return {
        "ok": True,
        "periods": len(period_rets), "rebalance_days": rebalance_days, "top_k": top_k,
        "lookback": lookback, "n_codes": m, "span_days": n,
        "strategy_return_pct": round(strat_total * 100, 1),
        "buyhold_return_pct": round(bh_total * 100, 1),
        "excess_pct": round((strat_total - bh_total) * 100, 1),
        "strategy_cagr_pct": round((strat_eq[-1] ** (1 / years) - 1) * 100, 1),
        "buyhold_cagr_pct": round((bh_eq[-1] ** (1 / years) - 1) * 100, 1),
        "strategy_maxdd_pct": round(_max_dd(strat_eq) * 100, 1),
        "buyhold_maxdd_pct": round(_max_dd(bh_eq) * 100, 1),
        "win_rate": round(sum(1 for r in period_rets if r > 0) / len(period_rets) * 100, 1),
        "avg_turnover_pct": round(sum(turnovers) / len(turnovers) * 100, 1),
        "beats_buyhold": strat_total > bh_total,
        "note": (f"{len(period_rets)}回リバランス（{rebalance_days}営業日毎・モメンタム上位{top_k}・"
                 f"コスト{cost_rate*100:.1f}%/回転）。戦略 {strat_total*100:+.1f}% vs buy&hold {bh_total*100:+.1f}%"
                 f"（超過 {(strat_total-bh_total)*100:+.1f}%）。回転コスト込みで買い持ちに"
                 + ("勝っています。" if strat_total > bh_total else "負けています＝過度な回転は逆効果の可能性。")),
    }


# =========================================================
# 出口・資金管理（損切り・ポジションサイズ・分割買い）— 決定論的
# =========================================================
# 入口（スクリーニング/診断）と分離した「出口層」。kenmo『5年で1億』の-8%損切り・
# DUKE 6章の5分割法/-10%損切り、片山『勝つ投資』のリスク管理（1トレードの損失を資金の
# 一定%に抑える）を、単一の真実として集約する。analyze_breakout_projection / analyze_position
# はここを参照する。


def build_tranche_plan(last_close: Optional[float], *, hard_stop_pct: float = -0.10) -> Optional[dict]:
    """5分割の打診買い→買い増し計画（DUKE 6章 / kenmo の資金管理）。

    資金を5分割し最初の1/5を試し玉に。含み益が出たら高値ブレイクの度に買い増し、
    含み損なら買い増さない。1回の損切りは hard_stop_pct（平均取得単価比、既定-10%）。
    """
    if not (last_close and last_close > 0):
        return None
    return {
        "method": "5分割法",
        "tranches": [
            {"no": 1, "ratio_pct": 20, "trigger": "試し玉（ピボット/現値）", "ref_price": round(last_close, 1)},
            {"no": 2, "ratio_pct": 20, "trigger": "含み益が出たら（+3%目安）", "ref_price": round(last_close * 1.03, 1)},
            {"no": 3, "ratio_pct": 20, "trigger": "上のボックス上抜けで（+7%目安）", "ref_price": round(last_close * 1.07, 1)},
            {"no": 4, "ratio_pct": 20, "trigger": "押し目→再度の高値更新で", "ref_price": None},
            {"no": 5, "ratio_pct": 20, "trigger": "新高値ブレイクの度に（任意）", "ref_price": None},
        ],
        "hard_stop_pct": round(hard_stop_pct * 100),
        "hard_stop_price": round(last_close * (1 + hard_stop_pct), 1),
        "note": ("資金を5分割し、最初の1/5を試し玉として打診買い。含み益が出たら高値ブレイクの度に"
                 "買い増し、含み損なら買い増さない。1回の損切りは最大"
                 f"{round(hard_stop_pct * 100)}%（平均取得単価比）。"
                 "※上の『損切り目安』(トレイル/ATR)は利を伸ばすための防御線で、"
                 f"{round(hard_stop_pct * 100)}%は新規買いの最終防衛ライン。"),
    }


def compute_position_size(
    capital: Optional[float], entry: Optional[float], stop: Optional[float],
    *, risk_per_trade: float = 0.01, max_position_pct: float = 0.20,
    lot_size: int = 100,
) -> dict:
    """1トレードの許容損失（資金 × risk_per_trade）とストップ幅から建玉数を逆算する。

    片山『勝つ投資』/kenmo のリスク管理思想を実装：「1回の損切りで失う額を資金の一定%
    （既定1%）に抑える」。建玉が資金の max_position_pct（既定20%）を超える場合は上限で頭打ち。
    日本株を想定し lot_size（既定100株）単位に丸める。
    """
    if not (capital and entry and stop and entry > 0 and stop > 0 and entry > stop):
        return {"ok": False,
                "reason": "建玉サイズの算出には 資金・エントリー価格・損切り価格（エントリー>損切り>0）が必要"}
    risk_amount = capital * risk_per_trade
    per_share_risk = entry - stop
    raw_shares = risk_amount / per_share_risk
    shares = int(raw_shares // lot_size) * lot_size
    capped = False
    cap_value = capital * max_position_pct
    if shares * entry > cap_value:
        shares = int((cap_value / entry) // lot_size) * lot_size
        capped = True
    position_value = shares * entry
    actual_risk = shares * per_share_risk
    return {
        "ok": shares > 0,
        "shares": shares,
        "position_value": round(position_value, 0),
        "position_pct": round(position_value / capital * 100, 1) if capital else None,
        "risk_amount": round(actual_risk, 0),
        "risk_pct_of_capital": round(actual_risk / capital * 100, 2) if capital else None,
        "stop_distance_pct": round(per_share_risk / entry * 100, 1),
        "capped_by_max_position": capped,
        "params": {"risk_per_trade_pct": round(risk_per_trade * 100, 2),
                   "max_position_pct": round(max_position_pct * 100), "lot_size": lot_size},
        "note": (f"資金の{risk_per_trade * 100:.0f}%（{risk_amount:,.0f}）を1トレードの許容損失とし、"
                 f"損切り幅{per_share_risk:,.0f}/株から{shares:,}株。"
                 + ("建玉が上限に達したため頭打ち。" if capped else "")),
    }


def evaluate_exit_signals(
    df,
    *,
    avg_cost: Optional[float] = None,
    hard_stop_pct: float = -0.08,
    trail_n: int = 22,
    trail_atr: float = 3.0,
    atr_n: int = 14,
    sma_mid: int = 75,
    sma_slow: int = 200,
) -> dict:
    """保有銘柄の出口（損切り・トレイリング）を統一判定する決定論的な「出口層」。

    複数の損切りルールを同時に評価し、発火したものと推奨アクションを返す：
      - ハード損切り: 平均取得単価比 hard_stop_pct（既定-8%、kenmo。-10%でDUKE）
      - トレイリング: シャンデリア・エグジット（直近 trail_n 日高値 − trail_atr×ATR）
      - 75日/200日MA 割れ（中期トレンド転換）

    保有後の手仕舞いに特化（入口判定は analyze_position 側）。OHLCV のみ。
    """
    import math

    if df is None or len(df) < 30:
        return {"ok": False, "error": "出口判定に十分な履歴がありません（約1.5ヶ月以上必要）"}
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

    atr_series = TechnicalSignals.atr(high, low, close, n=atr_n)
    last_atr = None
    for k in range(1, min(6, n) + 1):
        last_atr = _fin(atr_series.iloc[-k])
        if last_atr:
            break

    hh = _fin(high.tail(trail_n).max())
    trailing_stop = (hh - trail_atr * last_atr) if (hh and last_atr) else None
    sma_m = _fin(TechnicalSignals.sma(close, sma_mid).iloc[-1]) if n >= sma_mid else None
    sma_s = _fin(TechnicalSignals.sma(close, sma_slow).iloc[-1]) if n >= sma_slow else None

    hard_stop_price = None
    if avg_cost:
        ac = _fin(avg_cost)
        if ac and ac > 0:
            hard_stop_price = ac * (1 + hard_stop_pct)

    triggered = []
    if hard_stop_price is not None and last_close <= hard_stop_price:
        triggered.append({"rule": "hard_stop", "label": f"ハード損切り（取得単価比{round(hard_stop_pct * 100)}%）",
                          "level": round(hard_stop_price, 1)})
    if trailing_stop is not None and last_close < trailing_stop:
        triggered.append({"rule": "trailing_stop", "label": f"トレイリング割れ（{trail_n}日高値−{trail_atr:g}ATR）",
                          "level": round(trailing_stop, 1)})
    if sma_m is not None and last_close < sma_m:
        triggered.append({"rule": "sma_mid_break", "label": f"{sma_mid}日MA割れ", "level": round(sma_m, 1)})
    if sma_s is not None and last_close < sma_s:
        triggered.append({"rule": "sma_slow_break", "label": f"{sma_slow}日MA割れ", "level": round(sma_s, 1)})

    # アクション: ハード/トレイリング割れは即手仕舞い。MA割れ単独は縮小（一部利確）。
    hard_hit = any(t["rule"] in ("hard_stop", "trailing_stop") for t in triggered)
    ma_hit = any(t["rule"] in ("sma_mid_break", "sma_slow_break") for t in triggered)
    if hard_hit:
        action, note = "SELL", "損切り/トレイリングのストップ割れ。ルール通り手仕舞い（迷わず実行）。"
    elif ma_hit:
        action, note = "TRIM", "中期トレンドの節目割れ。一部利確して様子見、戻せなければ撤退。"
    else:
        action, note = "HOLD", "ストップは未抵触。トレイリングストップを切り上げて利を伸ばす。"

    pnl_pct = None
    if avg_cost:
        ac = _fin(avg_cost)
        if ac and ac > 0:
            pnl_pct = round((last_close - ac) / ac * 100, 1)

    return {
        "ok": True,
        "as_of": StyleStrategy._data_as_of(df),
        "last_close": round(last_close, 1),
        "atr": round(last_atr, 2) if last_atr else None,
        "stops": {
            "hard_stop": round(hard_stop_price, 1) if hard_stop_price else None,
            "hard_stop_pct": round(hard_stop_pct * 100),
            "trailing_stop": round(trailing_stop, 1) if trailing_stop else None,
            "sma_mid": round(sma_m, 1) if sma_m else None,
            "sma_slow": round(sma_s, 1) if sma_s else None,
        },
        "pnl_pct": pnl_pct,
        "triggered": triggered,
        "action": action,
        "action_label": _ACTION_LABELS.get(action, action),
        "note": note,
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
