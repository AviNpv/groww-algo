"""Pluggable intraday strategy: entry filters, sizing and exit management.

All thresholds come from config/strategy.yaml. Change the YAML, not the code.
"""
from __future__ import annotations

import datetime as dt

import numpy as np

from engine import ledger
from engine.broker import size_position
from engine.settings import cfg, now_ist, param


def _p(params: dict | None, section: str, key: str, default):
    return param(params, section, key, default)


def _parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now_ist().tzinfo)
    return parsed


def stop_distance(price: float, atr_abs: float) -> float:
    """Intraday stop: 60% of daily ATR, clamped to 0.4%-2.0% of price."""
    raw = atr_abs * 0.6 if atr_abs and np.isfinite(atr_abs) else price * 0.01
    return float(np.clip(raw, price * 0.004, price * 0.02))


def build_entry(c: dict, equity: float, cash: float, held: set[str],
                params: dict | None = None) -> dict:
    """Turn a scanned candidate into a sized BUY signal, or explain the rejection."""
    rejections: list[str] = []
    price = float(c.get("price") or 0)
    if price <= 0:
        return {"symbol": c.get("symbol"), "action": "SKIP", "rejections": ["no price"]}

    if c.get("symbol") in held:
        rejections.append("already holding this symbol")
    if c.get("status") != "QUALIFIED":
        rejections.append(f"status {c.get('status', 'UNKNOWN')}")
    if float(c.get("score", 0)) < float(_p(params, "entry", "min_score", 65)):
        rejections.append(f"score {c.get('score')} below threshold")
    if int(c.get("confirmations", 0)) < int(_p(params, "entry", "min_confirmations", 3)):
        rejections.append("not enough factor confirmations")

    indicators = c.get("indicators", {})
    ema20 = float(indicators.get("ema20") or 0)
    ema50 = float(indicators.get("ema50") or 0)
    vwap = float(indicators.get("vwap") or 0)
    rel_vol = float(indicators.get("rel_vol") or 0)
    atr_pct = float(indicators.get("atr_pct") or 0)

    if _p(params, "entry", "require_trend", True):
        if not (ema20 and price > ema20):
            rejections.append("price below EMA20")
        if not (ema20 and ema50 and ema20 > ema50):
            rejections.append("EMA20 below EMA50")
    if _p(params, "entry", "require_above_vwap", True) and vwap:
        if price < vwap:
            rejections.append("price below VWAP")
        extension = (price / vwap - 1) * 100
        if extension > float(_p(params, "entry", "max_vwap_extension_pct", 2.5)):
            rejections.append(f"too extended above VWAP ({extension:.1f}%)")
    if rel_vol < float(_p(params, "entry", "min_relative_volume", 1.4)):
        rejections.append(f"relative volume {rel_vol:.1f} too low")
    if atr_pct < float(_p(params, "entry", "min_atr_pct", 0.4)):
        rejections.append(f"ATR {atr_pct:.2f}% too low")
    if atr_pct > float(_p(params, "entry", "max_atr_pct", 3.5)):
        rejections.append(f"ATR {atr_pct:.2f}% too high")

    atr_abs = price * atr_pct / 100 if atr_pct else price * 0.01
    stop_dist = stop_distance(price, atr_abs)
    stop = round(price - stop_dist, 2)
    target_r = float(_p(params, "exit", "target_r_mult", 2.0))
    target = round(price + stop_dist * target_r, 2)

    resistance = c.get("resistance")
    if resistance and float(resistance) > price * 1.003:
        capped = round(min(target, float(resistance)), 2)
        if capped > price:
            target = capped

    risk_reward = round((target - price) / (price - stop), 2) if price > stop else 0.0
    if risk_reward < float(_p(params, "entry", "min_risk_reward", 1.8)):
        rejections.append(f"risk/reward {risk_reward} below minimum")

    qty = size_position(price, stop, equity, cash, params=params) if not rejections else 0
    if not rejections and qty < 1:
        rejections.append("position size rounded to zero (capital/risk too small)")

    if rejections:
        return {"symbol": c.get("symbol"), "action": "SKIP", "price": price,
                "score": c.get("score"), "rejections": rejections, "risk_reward": risk_reward}

    reason = f"{c.get('score')}/100 | " + ", ".join(
        (p.get("name") if isinstance(p, dict) else str(p)) for p in (c.get("patterns") or [])[:3]
    )
    return {
        "symbol": c["symbol"], "action": "BUY", "price": price,
        "stop": stop, "target": target, "qty": qty,
        "risk_amount": round((price - stop) * qty, 2),
        "risk_reward": risk_reward, "score": c.get("score"),
        "buckets": c.get("buckets", {}), "patterns": c.get("patterns", []),
        "status": c.get("status"), "rejections": [], "reason": reason.strip(" |"),
    }


def check_exit(pos: dict, price: float, now: dt.datetime | None = None,
               params: dict | None = None) -> dict:
    """Return {exit, reason, new_stop, r_multiple} for one open position."""
    now = now or now_ist()
    entry = float(pos["avg_price"])
    qty = int(pos["qty"])
    stop = float(pos.get("stop") or entry * 0.99)
    target = float(pos.get("target") or entry * 1.02)
    highest = max(float(pos.get("highest_price") or entry), price)
    initial_risk = float(pos.get("initial_risk") or (entry - stop) or 1)
    r_multiple = (price - entry) / initial_risk if initial_risk else 0.0

    # 1) hard square-off for MIS
    square_off = str(_p(params, "exit", "square_off", "15:20"))
    hh, mm = int(square_off[:2]), int(square_off[3:5])
    if now.hour > hh or (now.hour == hh and now.minute >= mm):
        return {"exit": True, "reason": "SQUARE_OFF", "new_stop": stop, "r_multiple": r_multiple}

    # 2) stop / target
    if price <= stop:
        reason = "TRAILING_STOP" if stop > entry else "STOP_LOSS"
        return {"exit": True, "reason": reason, "new_stop": stop, "r_multiple": r_multiple}
    if price >= target:
        return {"exit": True, "reason": "TARGET", "new_stop": stop, "r_multiple": r_multiple}

    # 3) time stop
    opened = _parse_ts(pos.get("opened_at"))
    minutes = int(_p(params, "exit", "time_stop_minutes", 180))
    if opened and (now - opened).total_seconds() / 60 >= minutes:
        return {"exit": True, "reason": "TIME_STOP", "new_stop": stop, "r_multiple": r_multiple}

    # 4) raise the stop to break-even, then trail
    new_stop = stop
    if r_multiple >= float(_p(params, "exit", "move_stop_to_be_at_r", 0.8)):
        new_stop = max(new_stop, entry)
    if r_multiple >= float(_p(params, "exit", "trail_after_r", 1.0)):
        atr_abs = initial_risk / max(float(_p(params, "exit", "stop_atr_mult", 1.2)), 0.1)
        trail = highest - atr_abs * float(_p(params, "exit", "trailing_atr_mult", 0.8))
        new_stop = max(new_stop, trail)
    new_stop = round(min(new_stop, price), 2)

    return {"exit": False, "reason": None, "new_stop": new_stop, "r_multiple": r_multiple}


def can_open_new(equity: float, params: dict | None = None) -> tuple[bool, str]:
    """Portfolio-level gates: position cap, daily loss limit, cash."""
    acc = ledger.account()
    open_count = len(ledger.positions())
    max_positions = int(_p(params, "account", "max_positions", 2))
    if open_count >= max_positions:
        return False, f"max {max_positions} open positions"

    max_loss = float(_p(params, "account", "max_daily_loss", 200))
    day_start = float(acc.get("day_start_equity") or equity)
    if equity - day_start <= -abs(max_loss):
        return False, f"daily loss limit hit ({equity - day_start:.0f})"

    if float(acc.get("cash", 0)) < 100:
        return False, "insufficient cash"
    return True, ""
