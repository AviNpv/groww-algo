"""V4 100-point scoring: trend, momentum, patterns, volume, market, quality, risk.

The seven authored V4 weights are shown verbatim in the UI. Internally the
market_sector and stock_quality weights are combined into one quality bucket
because the scanner's quality input already carries both fundamentals and
relative-strength context.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from engine import indicators as ind
from engine.settings import cfg, param

TREND_RAW = 22.0
MOMENTUM_RAW = 16.0
VOLUME_RAW = 14.0
RISK_RAW = 10.0
V4_WEIGHT_KEYS = ("price_pattern", "stock_quality", "market_sector", "risk_quality")


def _bucket(value: float, maximum: float) -> dict:
    safe = float(value) if np.isfinite(float(value)) else 0.0
    return {"score": round(safe, 1), "max": float(maximum),
            "pct": round(safe / maximum * 100, 1) if maximum else 0.0}


def _rule(params: dict | None, *path: str, default):
    """Read a nested rule from params first, then from strategy.yaml."""
    node = params
    for key in path:
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            node = None
            break
    if node is None:
        node = cfg(*path, default=default)
    return default if node is None else node


def _weights(params: dict | None) -> dict:
    weights = param(params, "scoring", "weights", {}) or {}
    if not isinstance(weights, dict) or not weights:
        weights = {}
    if any(key in weights for key in V4_WEIGHT_KEYS):
        return {
            "trend": float(weights.get("trend", 22)),
            "momentum": float(weights.get("momentum", 16)),
            "pattern": float(weights.get("price_pattern", 16)),
            "volume": float(weights.get("volume", 14)),
            "market_sector": float(weights.get("market_sector", 12)),
            "stock_quality": float(weights.get("stock_quality", 10)),
            "risk": float(weights.get("risk_quality", 10)),
        }
    return {
        "trend": float(weights.get("trend", 22)),
        "momentum": float(weights.get("momentum", 16)),
        "pattern": float(weights.get("pattern", 16)),
        "volume": float(weights.get("volume", 14)),
        "market_sector": 0.0,
        "stock_quality": 0.0,
        "risk": float(weights.get("risk", 10)),
    }


def _trend(daily: pd.DataFrame, intra: pd.DataFrame, quote: dict,
           trend_frame: pd.DataFrame | None = None) -> tuple[float, list[str]]:
    """EMA alignment (8), multi-timeframe (6), VWAP (4), price structure (4)."""
    notes: list[str] = []
    score = 0.0
    price = float(quote.get("price") or ind.last(intra, "c", 0.0))
    ema20 = ind.last(daily, "ema20", price)
    ema50 = ind.last(daily, "ema50", price)
    ema200 = ind.last(daily, "ema200", price)

    if ema20 and ema50 and ema200 and ema20 > ema50 > ema200:
        score += 8
        notes.append("Daily EMA20 > EMA50 > EMA200")
    elif ema20 and ema50 and ema20 > ema50:
        score += 4
        notes.append("Daily EMA20 > EMA50")

    if trend_frame is not None and len(trend_frame):
        t20 = ind.last(trend_frame, "ema20", price)
        t50 = ind.last(trend_frame, "ema50", price)
        t200 = ind.last(trend_frame, "ema200", price)
        if t20 and t50 and price > t20 > t50:
            score += 6
            notes.append("15m trend aligned with price")
            if t200 and t50 > t200:
                notes.append("15m EMA stack confirmed")
        elif t20 and t50 and t20 > t50:
            score += 3
            notes.append("15m EMA20 > EMA50")

    vwap = ind.last(intra, "vwap", price)
    if price > vwap > 0:
        score += 4
        notes.append("Price above VWAP")

    change = float(quote.get("change_pct", 0.0) or 0.0)
    ret5 = ind.last(daily, "ret5", 0.0)
    if change > 0:
        score += 2
        notes.append("Positive session move")
    if ret5 > 0:
        score += 2
        notes.append(f"5-day momentum +{ret5:.1f}%")
    return min(score, TREND_RAW), notes


def _momentum(daily: pd.DataFrame, intra: pd.DataFrame,
              params: dict | None = None) -> tuple[float, list[str]]:
    """RSI (5), MACD (4), ADX (4), acceleration (3) = V4 momentum weight."""
    notes: list[str] = []
    score = 0.0
    rsi_min = float(_rule(params, "entry", "momentum", "rsi_min", default=55))
    rsi_max = float(_rule(params, "entry", "momentum", "rsi_max", default=72))
    adx_min = float(_rule(params, "entry", "momentum", "adx_min", default=20))
    macd_required = bool(_rule(params, "entry", "momentum", "macd_bullish", default=True))

    rsi = ind.last(intra, "rsi14", ind.last(daily, "rsi14", 50.0))
    adx = ind.last(intra, "adx", ind.last(daily, "adx", 0.0))
    hist = ind.last(intra, "macd_hist", ind.last(daily, "macd_hist", 0.0))
    hist_prev = 0.0
    if intra is not None and len(intra) > 2 and "macd_hist" in intra:
        hist_prev = float(intra["macd_hist"].iloc[-2])

    if rsi_min <= rsi <= rsi_max:
        score += 5
        notes.append(f"RSI {rsi:.0f} in the V4 momentum band")
    elif rsi_min - 5 <= rsi <= rsi_max + 5:
        score += 3
        notes.append(f"RSI {rsi:.0f} near the momentum band")

    if hist > 0:
        score += 4
        notes.append("MACD bullish")
    elif macd_required:
        notes.append("MACD is not bullish")
    if adx >= adx_min:
        score += 4
        notes.append(f"Trend strength ADX {adx:.0f}")
    elif adx >= adx_min - 5:
        score += 2
        notes.append(f"ADX {adx:.0f} building")
    if hist > 0 and hist > hist_prev:
        score += 3
        notes.append("Momentum accelerating")
    return min(score, MOMENTUM_RAW), notes


def _volume(intra: pd.DataFrame, daily: pd.DataFrame, patterns: dict,
            params: dict | None = None) -> tuple[float, list[str]]:
    """Relative volume (8), time-of-day volume (4), volume trend (2)."""
    notes: list[str] = []
    score = 0.0
    min_rv = float(_rule(params, "entry", "volume", "relative_volume_min", default=1.5))
    use_tod = bool(_rule(params, "entry", "volume", "use_time_of_day_average", default=True))
    rel = ind.last(intra, "rel_vol", ind.last(daily, "rel_vol", 1.0))

    if rel >= min_rv:
        score += 8
        notes.append(f"Relative volume {rel:.1f}x >= {min_rv:g}x")
    elif rel >= 1.0:
        score += 5
        notes.append(f"Relative volume {rel:.1f}x")
    elif rel > 0:
        score += 1
        notes.append(f"Thin volume {rel:.1f}x")

    if use_tod and rel >= min_rv:
        score += 4
        notes.append("Volume confirms the time-of-day baseline")
    elif use_tod and rel >= 1.0:
        score += 2

    if intra is not None and len(intra) > 2 and "v" in intra:
        if float(intra["v"].iloc[-1]) > float(intra["v"].iloc[-2]):
            score += 2
            notes.append("Volume rising into the signal")
    if patterns.get("breakout") and rel >= min_rv:
        notes.append("Breakout volume confirmed")
    return min(score, VOLUME_RAW), notes


def _risk(daily: pd.DataFrame, patterns: dict, params: dict | None = None) -> tuple[float, list[str]]:
    """ATR suitability (4) + room-to-target risk quality (6)."""
    notes: list[str] = []
    score = 0.0
    min_atr = float(_rule(params, "entry", "volatility", "minimum_atr_pct", default=0.4))
    max_atr = float(_rule(params, "entry", "volatility", "maximum_atr_pct", default=3.5))
    atr_pct = ind.last(daily, "atr_pct", 2.0)

    if min_atr <= atr_pct <= max_atr:
        score += 4
        notes.append(f"ATR {atr_pct:.2f}% inside the V4 band")
    else:
        notes.append(f"ATR {atr_pct:.2f}% outside the V4 band")

    if patterns.get("breakout") and not patterns.get("near_resistance"):
        score += 6
        notes.append("Breakout with clear room to target")
    elif patterns.get("near_support") and not patterns.get("near_resistance"):
        score += 5
        notes.append("Holding above support")
    elif not patterns.get("near_resistance"):
        score += 4
        notes.append("Clear room to resistance")
    else:
        score += 1
        notes.append("Price close to resistance")
    return min(score, RISK_RAW), notes


def score_symbol(symbol: str, quote: dict, daily: pd.DataFrame, intra: pd.DataFrame,
                 patterns: dict, quality: dict, params: dict | None = None,
                 trend_frame: pd.DataFrame | None = None) -> dict:
    weights = _weights(params)
    max_market = weights["market_sector"]
    max_stock = weights["stock_quality"]
    max_quality = max_stock + max_market if (max_stock or max_market) else float(
        param(params, "scoring", "weights", {}).get("quality", 22) or 22)

    trend, trend_notes = _trend(daily, intra, quote, trend_frame)
    momentum, mom_notes = _momentum(daily, intra, params)
    volume, vol_notes = _volume(intra, daily, patterns, params)
    risk, risk_notes = _risk(daily, patterns, params)

    pattern_raw = float(patterns.get("pattern_score", 50.0) or 50.0)
    quality_raw = float(quality.get("score", 50.0) or 50.0)
    if not np.isfinite(pattern_raw):
        pattern_raw = 50.0
    if not np.isfinite(quality_raw):
        quality_raw = 50.0

    buckets = {
        "trend": _bucket(trend / TREND_RAW * weights["trend"], weights["trend"]),
        "momentum": _bucket(momentum / MOMENTUM_RAW * weights["momentum"], weights["momentum"]),
        "pattern": _bucket(pattern_raw / 100 * weights["pattern"], weights["pattern"]),
        "volume": _bucket(volume / VOLUME_RAW * weights["volume"], weights["volume"]),
        "quality": _bucket(quality_raw / 100 * max_quality, max_quality),
        "risk": _bucket(risk / RISK_RAW * weights["risk"], weights["risk"]),
    }
    total = round(float(np.clip(sum(b["score"] for b in buckets.values()), 0, 100)), 1)

    confirmations = sum(1 for name in ("trend", "momentum", "pattern", "volume", "quality")
                        if buckets[name]["pct"] >= 50)
    entry_at = float(_rule(params, "scoring", "thresholds", "entry",
                           default=cfg("entry", "min_score", default=60)))
    qualified_at = float(_rule(params, "scoring", "thresholds", "qualified", default=65))
    watch_at = float(_rule(params, "scoring", "thresholds", "watchlist", default=55))
    min_conf = int(_rule(params, "entry", "required_confirmations",
                         default=cfg("entry", "min_confirmations", default=5)))
    if total >= entry_at and confirmations >= min_conf:
        status = "QUALIFIED"
    elif total >= qualified_at:
        status = "WATCHLIST"
    elif total >= watch_at:
        status = "WATCHLIST"
    else:
        status = "REJECT"

    reasons = trend_notes + mom_notes + vol_notes + risk_notes
    for hit in patterns.get("patterns", [])[:4]:
        reasons.append(f"{hit['name']}: {hit['note']}")

    price = float(quote.get("price") or ind.last(intra, "c", 0.0))
    rsi = ind.last(intra, "rsi14", ind.last(daily, "rsi14", 50.0))
    rel = ind.last(intra, "rel_vol", ind.last(daily, "rel_vol", 1.0))
    return {
        "symbol": symbol,
        "price": round(price, 2),
        "change_pct": round(float(quote.get("change_pct", 0.0)), 2),
        "volume": quote.get("volume"),
        "score": total,
        "status": status,
        "confirmations": confirmations,
        "buckets": buckets,
        "patterns": patterns.get("patterns", []),
        "support": patterns.get("support"),
        "resistance": patterns.get("resistance"),
        "breakout": bool(patterns.get("breakout")),
        "quality": quality,
        "indicators": {
            "rsi": round(rsi, 1),
            "atr_pct": round(ind.last(daily, "atr_pct", 0.0), 2),
            "rel_vol": round(rel, 2),
            "vwap": round(ind.last(intra, "vwap", price), 2),
            "ema20": round(ind.last(daily, "ema20", price), 2),
            "ema50": round(ind.last(daily, "ema50", price), 2),
            "ema200": round(ind.last(daily, "ema200", price), 2),
            "adx": round(ind.last(daily, "adx", 0.0), 1),
            "ret5": round(ind.last(daily, "ret5", 0.0), 2),
            "pos_52w": round(ind.last(daily, "pos_52w", 50.0), 1),
        },
        "reasons": reasons,
    }
