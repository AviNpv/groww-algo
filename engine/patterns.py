"""Candlestick + chart-pattern detection on plain pandas frames.

Every detector is heuristic. That is the nature of pattern scanning: it
produces candidates, not certainties. Confidence values are relative, not
probabilities of profit.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd


@dataclass
class PatternHit:
    name: str
    direction: str          # bullish | bearish | neutral
    confidence: float       # 0..1
    note: str

    def as_dict(self) -> dict:
        return asdict(self)


def _body(row) -> float:
    return abs(float(row["c"]) - float(row["o"]))


def _range(row) -> float:
    return max(float(row["h"]) - float(row["l"]), 1e-9)


def _upper_shadow(row) -> float:
    return float(row["h"]) - max(float(row["o"]), float(row["c"]))


def _lower_shadow(row) -> float:
    return min(float(row["o"]), float(row["c"])) - float(row["l"])


def detect_candles(df: pd.DataFrame, max_hits: int = 6) -> list[PatternHit]:
    if df is None or len(df) < 3:
        return []
    cur = df.iloc[-1]
    prev = df.iloc[-2]
    body = _body(cur)
    rng = _range(cur)
    hits: list[PatternHit] = []

    # Bullish engulfing
    if (prev["c"] < prev["o"] and cur["c"] > cur["o"]
            and cur["c"] >= prev["o"] and cur["o"] <= prev["c"] and body > _body(prev)):
        hits.append(PatternHit("Bullish engulfing", "bullish", 0.72,
                               "Green body fully engulfs the previous red body"))

    # Bearish engulfing
    if (prev["c"] > prev["o"] and cur["c"] < cur["o"]
            and cur["o"] >= prev["c"] and cur["c"] <= prev["o"] and body > _body(prev)):
        hits.append(PatternHit("Bearish engulfing", "bearish", 0.7,
                               "Red body fully engulfs the previous green body"))

    # Hammer / shooting star
    if body > 0 and _lower_shadow(cur) >= 2 * body and _upper_shadow(cur) <= body * 0.6:
        hits.append(PatternHit("Hammer", "bullish", 0.6,
                               "Long lower wick rejects the lows"))
    if body > 0 and _upper_shadow(cur) >= 2 * body and _lower_shadow(cur) <= body * 0.6:
        hits.append(PatternHit("Shooting star", "bearish", 0.58,
                               "Long upper wick rejects the highs"))

    # Doji
    if rng > 0 and body <= 0.1 * rng:
        hits.append(PatternHit("Doji", "neutral", 0.4, "Indecision candle"))

    # Marubozu
    if rng > 0 and body >= 0.85 * rng:
        direction = "bullish" if cur["c"] > cur["o"] else "bearish"
        hits.append(PatternHit("Marubozu", direction, 0.55,
                               "Full-body candle, strong conviction"))

    # Bullish harami
    if (prev["c"] < prev["o"] and cur["c"] > cur["o"]
            and cur["o"] > prev["c"] and cur["c"] < prev["o"]):
        hits.append(PatternHit("Bullish harami", "bullish", 0.5,
                               "Small green body inside the prior red body"))

    return hits[:max_hits]


def _slope(values: np.ndarray) -> float:
    if len(values) < 2:
        return 0.0
    x = np.arange(len(values), dtype=float)
    return float(np.polyfit(x, values, 1)[0])


def detect_breakout(df: pd.DataFrame, lookback: int = 20, vol_confirm: float = 1.3) -> PatternHit | None:
    if df is None or len(df) < lookback + 2:
        return None
    prior = df.iloc[-lookback - 1:-1]
    level = float(prior["h"].max())
    close = float(df["c"].iloc[-1])
    rel_vol = float(df["rel_vol"].iloc[-1]) if "rel_vol" in df else 1.0
    if close > level and rel_vol >= vol_confirm:
        strength = min(1.0, 0.55 + (rel_vol - vol_confirm) * 0.15)
        return PatternHit("Breakout", "bullish", round(strength, 2),
                          f"Closed above {lookback}-bar high {level:.2f} on {rel_vol:.1f}x volume")
    return None


def detect_breakdown(df: pd.DataFrame, lookback: int = 20) -> PatternHit | None:
    if df is None or len(df) < lookback + 2:
        return None
    level = float(df.iloc[-lookback - 1:-1]["l"].min())
    close = float(df["c"].iloc[-1])
    if close < level:
        return PatternHit("Breakdown", "bearish", 0.6, f"Closed below {lookback}-bar low {level:.2f}")
    return None


def detect_bull_flag(df: pd.DataFrame) -> PatternHit | None:
    if df is None or len(df) < 12:
        return None
    close = float(df["c"].iloc[-1])
    pole_start = float(df["c"].iloc[-12])
    if pole_start <= 0:
        return None
    pole = (close / pole_start - 1) * 100
    consolidation = df.iloc[-5:]
    cons_range = (float(consolidation["h"].max()) / float(consolidation["l"].min()) - 1) * 100
    cons_return = (float(consolidation["c"].iloc[-1]) / float(consolidation["c"].iloc[0]) - 1) * 100
    vol_drop = True
    if "v" in consolidation and float(consolidation["v"].mean()) > 0:
        prior_vol = float(df.iloc[-12:-5]["v"].mean() or 0)
        vol_drop = float(consolidation["v"].mean()) <= prior_vol * 1.05 if prior_vol else True
    if pole >= 5 and cons_range <= max(3.0, pole * 0.5) and abs(cons_return) <= 2.5 and vol_drop:
        return PatternHit("Bull flag", "bullish", 0.66,
                          f"{pole:.1f}% pole then tight {cons_range:.1f}% consolidation")
    return None


def detect_ascending_triangle(df: pd.DataFrame, window: int = 24) -> PatternHit | None:
    if df is None or len(df) < window:
        return None
    part = df.iloc[-window:]
    highs = part["h"].to_numpy(dtype=float)
    lows = part["l"].to_numpy(dtype=float)
    high_slope = _slope(highs)
    low_slope = _slope(lows)
    high_flat = abs(high_slope) / (float(np.mean(highs)) or 1) < 0.0015
    if high_flat and low_slope > 0:
        return PatternHit("Ascending triangle", "bullish", 0.6,
                          "Flat resistance with rising lows")
    return None


def detect_double_bottom(df: pd.DataFrame, window: int = 45) -> PatternHit | None:
    if df is None or len(df) < window:
        return None
    part = df.iloc[-window:]
    lows = part["l"].to_numpy(dtype=float)
    first = int(np.argmin(lows))
    if first < 3 or first > len(lows) - 6:
        return None
    tail = lows[first + 4:]
    if len(tail) < 4:
        return None
    second_rel = int(np.argmin(tail))
    second = first + 4 + second_rel
    low1, low2 = lows[first], lows[second]
    if low1 <= 0:
        return None
    if abs(low1 - low2) / low1 <= 0.035:
        neckline = float(part["h"].iloc[first:second + 1].max())
        close = float(part["c"].iloc[-1])
        if close >= neckline * 0.985:
            return PatternHit("Double bottom", "bullish", 0.65,
                              f"Two lows near {low1:.2f}, neckline {neckline:.2f}")
        return PatternHit("Double bottom forming", "neutral", 0.45,
                          f"Two lows near {low1:.2f}, watching neckline {neckline:.2f}")
    return None


def detect_head_shoulders(df: pd.DataFrame, window: int = 60) -> PatternHit | None:
    if df is None or len(df) < window:
        return None
    part = df.iloc[-window:]
    highs = part["h"].to_numpy(dtype=float)
    if len(highs) < 20:
        return None
    third = len(highs) // 3
    left = int(np.argmax(highs[:third]))
    head = int(np.argmax(highs[third:2 * third])) + third
    right = int(np.argmax(highs[2 * third:])) + 2 * third
    lh, hh, rh = highs[left], highs[head], highs[right]
    if hh > lh * 1.02 and hh > rh * 1.02 and abs(lh - rh) / hh <= 0.05:
        return PatternHit("Head and shoulders", "bearish", 0.62,
                          "Three peaks with a dominant middle head")
    return None


def detect_cup_handle(df: pd.DataFrame, window: int = 60) -> PatternHit | None:
    if df is None or len(df) < window:
        return None
    part = df.iloc[-window:]
    close = float(part["c"].iloc[-1])
    left_rim = float(part["h"].iloc[:max(1, window // 6)].max())
    bottom_idx = int(np.argmin(part["l"].to_numpy(dtype=float)))
    cup_depth = (left_rim - float(part["l"].iloc[bottom_idx])) / left_rim if left_rim else 0
    handle = part.iloc[-6:]
    handle_drop = (float(handle["l"].min()) / close - 1) * 100 if close else 0
    if 0.08 <= cup_depth <= 0.45 and -8 <= handle_drop <= -1 and close >= left_rim * 0.9:
        return PatternHit("Cup and handle", "bullish", 0.58,
                          f"Cup depth {cup_depth * 100:.0f}%, handle pullback {handle_drop:.1f}%")
    return None


def support_resistance(df: pd.DataFrame, lookback: int = 60) -> dict:
    """Cluster recent pivots into support/resistance levels."""
    if df is None or len(df) < 15:
        return {"support": None, "resistance": None, "near_support": False,
                "near_resistance": False, "levels": []}
    part = df.iloc[-lookback:]
    highs = part["h"].to_numpy(dtype=float)
    lows = part["l"].to_numpy(dtype=float)
    close = float(part["c"].iloc[-1])

    pivots: list[float] = []
    for i in range(2, len(part) - 2):
        if highs[i] >= max(highs[i - 2], highs[i - 1], highs[i + 1], highs[i + 2]):
            pivots.append(highs[i])
        if lows[i] <= min(lows[i - 2], lows[i - 1], lows[i + 1], lows[i + 2]):
            pivots.append(lows[i])
    pivots.sort()
    levels: list[float] = []
    for value in pivots:
        if not levels or abs(value - levels[-1]) / value > 0.008:
            levels.append(value)
        else:
            levels[-1] = (levels[-1] + value) / 2

    supports = [x for x in levels if x < close]
    resistances = [x for x in levels if x > close]
    support = max(supports) if supports else None
    resistance = min(resistances) if resistances else None
    return {
        "support": round(support, 2) if support else None,
        "resistance": round(resistance, 2) if resistance else None,
        "near_support": bool(support and abs(close / support - 1) <= 0.012),
        "near_resistance": bool(resistance and abs(resistance / close - 1) <= 0.012),
        "levels": [round(x, 2) for x in levels[-8:]],
    }


def detect_all(daily: pd.DataFrame | None, intraday: pd.DataFrame | None,
               lookback: int = 20) -> dict:
    """Run every detector and return hits + a 0-100 pattern score."""
    frames = [f for f in (intraday, daily) if f is not None and len(f) >= 5]
    hits: list[PatternHit] = []
    for frame in frames[:1]:
        hits.extend(detect_candles(frame))

    chart_frame = intraday if intraday is not None and len(intraday) >= 25 else daily
    if chart_frame is not None and len(chart_frame) >= 10:
        for detector in (
            lambda f: detect_breakout(f, lookback),
            detect_breakdown,
            detect_bull_flag,
            detect_ascending_triangle,
            detect_double_bottom,
            detect_head_shoulders,
            detect_cup_handle,
        ):
            try:
                hit = detector(chart_frame)
            except Exception:
                hit = None
            if hit:
                hits.append(hit)

    # daily-context patterns are useful even when the 5m frame is the entry frame
    if daily is not None and chart_frame is not daily and len(daily) >= 30:
        for detector in (detect_double_bottom, detect_cup_handle, detect_head_shoulders):
            try:
                hit = detector(daily)
            except Exception:
                hit = None
            if hit:
                hits.append(hit)

    seen: set[str] = set()
    unique: list[PatternHit] = []
    for hit in hits:
        if hit.name not in seen:
            seen.add(hit.name)
            unique.append(hit)

    sr = support_resistance(chart_frame if chart_frame is not None else daily, 60)
    score = 50.0
    for hit in unique:
        if hit.direction == "bullish":
            score += 12 * hit.confidence
        elif hit.direction == "bearish":
            score -= 14 * hit.confidence
    if sr["near_support"]:
        score += 6
    if sr["near_resistance"]:
        score -= 4
    score = float(np.clip(score, 0, 100))

    return {
        "patterns": [h.as_dict() for h in unique],
        "pattern_score": round(score, 1),
        "breakout": any(h.name == "Breakout" for h in unique),
        "bullish_count": sum(1 for h in unique if h.direction == "bullish"),
        "bearish_count": sum(1 for h in unique if h.direction == "bearish"),
        **sr,
    }
