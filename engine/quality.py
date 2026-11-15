"""Quality / liquidity / trend-persistence scoring.

Groww's API has no fundamentals endpoint, so the base quality score is built
from price and volume behaviour (which Groww does provide). When yfinance
fundamentals are available they are blended in; when they are not, the
technical quality score stands on its own and says so.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from engine import indicators as ind


def _clip(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return float(np.clip(value, lo, hi))


def _lin(value: float, low: float, high: float) -> float:
    """Map low..high onto 0..100 with clipping."""
    if high == low:
        return 50.0
    return _clip((value - low) / (high - low) * 100)


def fundamental_score(f: dict | None) -> tuple[float | None, dict]:
    if not f:
        return None, {}

    parts: dict[str, float] = {}

    roe = f.get("roe")
    if roe is not None:
        parts["roe"] = _lin(float(roe) * 100, -5, 25)

    margin = f.get("profit_margin")
    if margin is not None:
        parts["margin"] = _lin(float(margin) * 100, -5, 25)

    growth = f.get("revenue_growth")
    if growth is not None:
        parts["growth"] = _lin(float(growth) * 100, -10, 30)

    debt = f.get("debt_to_equity")
    if debt is not None:
        d = float(debt)
        if d > 5:      # yfinance sometimes reports percent
            d /= 100.0
        parts["debt"] = _clip(100 - _lin(d, 0.0, 2.0))

    pe = f.get("pe")
    if pe is not None and float(pe) > 0:
        p = float(pe)
        if p <= 20:
            parts["valuation"] = 85.0
        elif p <= 35:
            parts["valuation"] = 65.0
        elif p <= 60:
            parts["valuation"] = 45.0
        else:
            parts["valuation"] = 26.0

    if not parts:
        return None, {}
    return round(float(np.mean(list(parts.values()))), 1), {k: round(v, 1) for k, v in parts.items()}


def quality_score(daily: pd.DataFrame | None, fundamentals: dict | None = None) -> dict:
    """Return {score, label, components, fundamentals_used}."""
    if daily is None or len(daily) < 30:
        score, components = 45.0, {"data": 45.0}
        label = "Unknown"
    else:
        close = daily["c"]
        volume = daily["v"]

        # 1) liquidity: average daily traded value over 20 sessions
        turnover = float((close * volume).tail(20).mean() or 0)
        liquidity = _lin(np.log10(max(turnover, 1)), 6.5, 9.5)  # 3cr -> 300cr

        # 2) trend persistence: share of the last 100 closes above EMA50
        ema50 = daily["ema50"] if "ema50" in daily else ind.ema(close, 50)
        window = min(100, len(close))
        above = (close.tail(window) > ema50.tail(window)).mean()
        persistence = _clip(float(above) * 100)

        # 3) drawdown over the last 120 sessions (smaller is better)
        recent = close.tail(120)
        peak = recent.cummax()
        max_dd = float(((recent / peak) - 1).min() * 100) if len(recent) else -20.0
        drawdown = _clip(100 + max_dd * 3.3)  # -0% ->100, -30% -> ~0

        # 4) volatility stability: ATR% sweet spot 0.6-2.5
        atr_pct = ind.last(daily, "atr_pct", 2.0)
        if 0.6 <= atr_pct <= 2.5:
            stability = 90.0
        elif atr_pct < 0.6:
            stability = _clip(45 + atr_pct * 60)
        else:
            stability = _clip(90 - (atr_pct - 2.5) * 18)

        # 5) position in the 52-week range
        pos = ind.last(daily, "pos_52w", 50.0)
        range_score = 40 + 0.6 * pos if pos >= 50 else 30 + 0.6 * pos

        components = {
            "liquidity": round(liquidity, 1),
            "trend_persistence": round(persistence, 1),
            "drawdown": round(drawdown, 1),
            "stability": round(stability, 1),
            "range_position": round(_clip(range_score), 1),
        }
        score = (
            liquidity * 0.25
            + persistence * 0.30
            + drawdown * 0.20
            + stability * 0.10
            + range_score * 0.15
        )

    fscore, fparts = fundamental_score(fundamentals)
    if fscore is not None:
        score = score * 0.65 + fscore * 0.35
        components["fundamentals"] = fscore
        components.update({f"fund_{k}": v for k, v in fparts.items()})

    score = round(_clip(score), 1)
    if score >= 75:
        label = "High quality"
    elif score >= 60:
        label = "Good"
    elif score >= 45:
        label = "Average"
    else:
        label = "Weak"

    return {
        "score": score,
        "label": label,
        "components": components,
        "fundamentals_used": fscore is not None,
        "fundamentals": fundamentals or {},
    }
