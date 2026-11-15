"""Composite multi-factor scoring: trend, momentum, pattern, volume, quality, risk."""
from __future__ import annotations

import numpy as np
import pandas as pd

from engine import indicators as ind
from engine.settings import param


def _bucket(value: float, maximum: float) -> dict:
    return {"score": round(float(value), 1), "max": float(maximum),
            "pct": round(float(value) / maximum * 100, 1) if maximum else 0.0}


def _trend(daily: pd.DataFrame, intra: pd.DataFrame, quote: dict) -> tuple[float, list[str]]:
    notes: list[str] = []
    score = 0.0
    price = float(quote.get("price") or ind.last(intra, "c", 0.0))
    ema20 = ind.last(daily, "ema20", price)
    ema50 = ind.last(daily, "ema50", price)
    ema200 = ind.last(daily, "ema200", price)
    adx = ind.last(daily, "adx", 0.0)

    if price > ema20:
        score += 5
        notes.append("Above EMA20")
    if ema20 > ema50:
        score += 5
        notes.append("EMA20 > EMA50")
    if ema50 > ema200:
        score += 4
        notes.append("EMA50 > EMA200")
    vwap = ind.last(intra, "vwap", price)
    if price > vwap > 0:
        score += 4
        notes.append("Above VWAP")
    if adx >= 25:
        score += 4
        notes.append(f"Strong trend (ADX {adx:.0f})")
    elif adx >= 18:
        score += 2
        notes.append(f"Trend building (ADX {adx:.0f})")
    return min(score, 22.0), notes


def _momentum(daily: pd.DataFrame, intra: pd.DataFrame) -> tuple[float, list[str]]:
    notes: list[str] = []
    score = 0.0
    rsi = ind.last(intra, "rsi14", ind.last(daily, "rsi14", 50.0))
    hist = ind.last(intra, "macd_hist", ind.last(daily, "macd_hist", 0.0))
    hist_prev = 0.0
    if intra is not None and len(intra) > 2 and "macd_hist" in intra:
        hist_prev = float(intra["macd_hist"].iloc[-2])
    roc5 = ind.last(daily, "roc5", 0.0)

    if 50 <= rsi <= 70:
        score += 8
        notes.append(f"RSI {rsi:.0f} in the momentum zone")
    elif 45 <= rsi < 50 or 70 < rsi <= 76:
        score += 5
        notes.append(f"RSI {rsi:.0f} acceptable")
    elif rsi > 80:
        score += 1
        notes.append(f"RSI {rsi:.0f} overbought")
    else:
        notes.append(f"RSI {rsi:.0f} weak")

    if hist > 0:
        score += 6
        notes.append("MACD histogram positive")
        if hist > hist_prev:
            score += 2
            notes.append("MACD accelerating")
    if roc5 > 0:
        score += 4
        notes.append(f"5-day return +{roc5:.1f}%")
    return min(score, 20.0), notes


def _volume(intra: pd.DataFrame, daily: pd.DataFrame, patterns: dict) -> tuple[float, list[str]]:
    notes: list[str] = []
    score = 0.0
    rel = ind.last(intra, "rel_vol", ind.last(daily, "rel_vol", 1.0))
    if 1.5 <= rel <= 4.0:
        score += 8
        notes.append(f"Volume {rel:.1f}x average")
    elif 1.0 <= rel < 1.5:
        score += 5
        notes.append(f"Volume {rel:.1f}x average")
    elif rel > 4.0:
        score += 6
        notes.append(f"Volume spike {rel:.1f}x (climax risk)")
    else:
        score += 1
        notes.append(f"Thin volume {rel:.1f}x")

    if patterns.get("breakout") and rel >= 1.3:
        score += 5
        notes.append("Breakout confirmed by volume")
    return min(score, 13.0), notes


def _risk(daily: pd.DataFrame, patterns: dict) -> tuple[float, list[str]]:
    notes: list[str] = []
    score = 0.0
    atr_pct = ind.last(daily, "atr_pct", ind.last(daily, "atr_pct", 2.0))
    if 0.8 <= atr_pct <= 2.4:
        score += 5
        notes.append(f"Tradable volatility (ATR {atr_pct:.1f}%)")
    elif 0.4 <= atr_pct < 0.8:
        score += 2.5
        notes.append(f"Low volatility (ATR {atr_pct:.1f}%)")
    elif 2.4 < atr_pct <= 3.5:
        score += 3
        notes.append(f"High volatility (ATR {atr_pct:.1f}%)")
    else:
        notes.append(f"Unfavourable volatility (ATR {atr_pct:.1f}%)")

    if patterns.get("near_resistance"):
        notes.append("Price sitting under resistance")
    else:
        score += 3
        notes.append("Clear room to resistance")

    if patterns.get("near_support"):
        score += 2
        notes.append("Holding above support")
    return min(score, 10.0), notes


def score_symbol(symbol: str, quote: dict, daily: pd.DataFrame, intra: pd.DataFrame,
                 patterns: dict, quality: dict,
                 params: dict | None = None) -> dict:
    weights = param(params, "scoring", "weights", {}) or {}
    max_trend = float(weights.get("trend", 22))
    max_mom = float(weights.get("momentum", 20))
    max_pat = float(weights.get("pattern", 20))
    max_vol = float(weights.get("volume", 13))
    max_qual = float(weights.get("quality", 15))
    max_risk = float(weights.get("risk", 10))

    trend, trend_notes = _trend(daily, intra, quote)
    momentum, mom_notes = _momentum(daily, intra)
    volume, vol_notes = _volume(intra, daily, patterns)
    risk, risk_notes = _risk(daily, patterns)

    pattern_raw = float(patterns.get("pattern_score", 50.0))
    quality_raw = float(quality.get("score", 50.0))

    buckets = {
        "trend": _bucket(trend / 22 * max_trend if max_trend != 22 else trend, max_trend),
        "momentum": _bucket(momentum / 20 * max_mom if max_mom != 20 else momentum, max_mom),
        "pattern": _bucket(pattern_raw / 100 * max_pat, max_pat),
        "volume": _bucket(volume / 13 * max_vol if max_vol != 13 else volume, max_vol),
        "quality": _bucket(quality_raw / 100 * max_qual, max_qual),
        "risk": _bucket(risk / 10 * max_risk if max_risk != 10 else risk, max_risk),
    }
    total = sum(b["score"] for b in buckets.values())
    total = round(float(np.clip(total, 0, 100)), 1)

    confirmations = sum(1 for name in ("trend", "momentum", "pattern", "volume", "quality")
                        if buckets[name]["pct"] >= 50)
    qualified_at = float(param(params, "scoring", "qualified_score", 65))
    watch_at = float(param(params, "scoring", "watchlist_score", 55))
    if total >= qualified_at and confirmations >= int(param(params, "entry", "min_confirmations", 3)):
        status = "QUALIFIED"
    elif total >= watch_at:
        status = "WATCHLIST"
    else:
        status = "REJECT"

    reasons = trend_notes + mom_notes + vol_notes + risk_notes
    for hit in patterns.get("patterns", [])[:4]:
        reasons.append(f"{hit['name']}: {hit['note']}")

    price = float(quote.get("price") or ind.last(intra, "c", 0.0))
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
            "rsi": round(ind.last(intra, "rsi14", ind.last(daily, "rsi14", 50.0)), 1),
            "atr_pct": round(ind.last(daily, "atr_pct", 0.0), 2),
            "rel_vol": round(ind.last(intra, "rel_vol", 1.0), 2),
            "vwap": round(ind.last(intra, "vwap", price), 2),
            "ema20": round(ind.last(daily, "ema20", price), 2),
            "ema50": round(ind.last(daily, "ema50", price), 2),
            "adx": round(ind.last(daily, "adx", 0.0), 1),
            "ret5": round(ind.last(daily, "ret5", 0.0), 2),
            "pos_52w": round(ind.last(daily, "pos_52w", 50.0), 1),
        },
        "reasons": reasons,
    }
