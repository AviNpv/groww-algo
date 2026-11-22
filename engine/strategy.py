"""V4 intraday strategy: entry gates, risk sizing and exit management.

All thresholds come from config/strategy.yaml. V4 nested keys are read first;
legacy aliases exist only for older saved presets and the backtester.
"""
from __future__ import annotations

import datetime as dt

import numpy as np

from engine import ledger
from engine.broker import size_position
from engine.settings import cfg, now_ist, param


def _p(params: dict | None, section: str, key: str, default):
    return param(params, section, key, default)


def _rule(params: dict | None, *path: str, default):
    """Read a nested V4 rule from params first, then from strategy.yaml."""
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


def stop_distance(price: float, atr_abs: float,
                  params: dict | None = None) -> float:
    """V4 initial stop: ATR multiple clamped between configured percentages."""
    multiplier = float(_rule(params, "exit", "initial_stop_loss", "atr_multiplier",
                             default=cfg("exit", "stop_atr_mult", default=1.2)))
    min_pct = float(_rule(params, "exit", "initial_stop_loss", "minimum_stop_pct",
                          default=cfg("exit", "min_stop_pct", default=0.4)))
    max_pct = float(_rule(params, "exit", "initial_stop_loss", "maximum_stop_pct",
                          default=cfg("exit", "max_stop_pct", default=2.0)))
    raw = atr_abs * multiplier if atr_abs and np.isfinite(atr_abs) else price * 0.01
    return float(np.clip(raw, price * min_pct / 100, price * max_pct / 100))


def symbol_trade_count(symbol: str) -> int:
    """Trades already opened today for one symbol (V4 per-symbol daily cap)."""
    today = now_ist().date().isoformat()
    target = str(symbol).upper().replace(".NS", "")
    return sum(
        1 for t in ledger.trades(limit=500)
        if str(t.get("symbol", "")).upper().replace(".NS", "") == target
        and str(t.get("opened_at") or "").startswith(today)
    )


def build_entry(c: dict, equity: float, cash: float, held: set[str],
                params: dict | None = None) -> dict:
    """Turn a scanned candidate into a sized BUY signal, or explain the rejection."""
    rejections: list[str] = []
    price = float(c.get("price") or 0)
    if price <= 0:
        return {"symbol": c.get("symbol"), "action": "SKIP", "rejections": ["no price"]}

    if c.get("symbol") in held:
        rejections.append("already holding this symbol")
    if symbol_trade_count(str(c.get("symbol", ""))) >= int(
            _p(params, "account", "max_trades_per_symbol_per_day", 1)):
        rejections.append("daily trade limit reached for this symbol")
    if c.get("status") != "QUALIFIED":
        rejections.append(f"status {c.get('status', 'UNKNOWN')}")
    if float(c.get("score", 0)) < float(_p(params, "entry", "min_score", 85)):
        rejections.append(f"score {c.get('score')} below V4 minimum")
    if int(c.get("confirmations", 0)) < int(
            _rule(params, "entry", "required_confirmations",
                  default=cfg("entry", "min_confirmations", default=5))):
        rejections.append("not enough factor confirmations")

    if _rule(params, "scoring", "mandatory_gates", "groww_confirmation", default=True):
        source = str(c.get("data_source") or "").lower()
        if source != "groww" or bool(c.get("delayed", True)):
            rejections.append("Groww entry confirmation unavailable (Yahoo is scan-only)")
    if bool(c.get("data_stale")):
        rejections.append("stale quote rejected by V4 data validation")

    indicators = c.get("indicators", {})
    ema20 = float(indicators.get("ema20") or 0)
    ema50 = float(indicators.get("ema50") or 0)
    ema200 = float(indicators.get("ema200") or 0)
    vwap = float(indicators.get("vwap") or 0)
    rel_vol = float(indicators.get("rel_vol") or 0)
    atr_pct = float(indicators.get("atr_pct") or 0)
    rsi = float(indicators.get("rsi") or 50)
    adx = float(indicators.get("adx") or 0)

    if bool(_rule(params, "entry", "trend", "ema_alignment", default=True)):
        if not (ema20 and price > ema20):
            rejections.append("price below EMA20")
        if not (ema20 and ema50 and ema20 > ema50):
            rejections.append("EMA20 below EMA50")
        if ema200 and ema50 < ema200:
            rejections.append("EMA50 below EMA200")

    confirmation = c.get("trend_confirmation", {}) or {}
    if bool(_rule(params, "entry", "trend", "require_5m_confirmation", default=True)):
        if not bool(confirmation.get("5m")):
            rejections.append("5m EMA20 > EMA50 confirmation missing")
    if bool(_rule(params, "entry", "trend", "require_15m_confirmation", default=True)):
        if not bool(confirmation.get("15m")):
            rejections.append("15m EMA stack confirmation missing")

    if bool(_rule(params, "entry", "trend", "price_above_vwap", default=True)) and vwap:
        if price < vwap:
            rejections.append("price below VWAP")
        extension = (price / vwap - 1) * 100
        if extension > float(_p(params, "entry", "max_vwap_extension_pct", 2.5)):
            rejections.append(f"too extended above VWAP ({extension:.1f}%)")

    rsi_min = float(_rule(params, "entry", "momentum", "rsi_min", default=55))
    rsi_max = float(_rule(params, "entry", "momentum", "rsi_max", default=72))
    adx_min = float(_rule(params, "entry", "momentum", "adx_min", default=20))
    if not (rsi_min <= rsi <= rsi_max):
        rejections.append(f"RSI {rsi:.1f} outside V4 band {rsi_min:g}-{rsi_max:g}")
    if adx < adx_min:
        rejections.append(f"ADX {adx:.1f} below {adx_min:g}")

    min_rv = float(_rule(params, "entry", "volume", "relative_volume_min",
                         default=cfg("entry", "min_relative_volume", default=1.5)))
    if rel_vol < min_rv:
        rejections.append(f"relative volume {rel_vol:.1f} below {min_rv:g}x")
    min_atr = float(_rule(params, "entry", "volatility", "minimum_atr_pct", default=0.4))
    max_atr = float(_rule(params, "entry", "volatility", "maximum_atr_pct", default=3.5))
    if atr_pct < min_atr:
        rejections.append(f"ATR {atr_pct:.2f}% too low")
    if atr_pct > max_atr:
        rejections.append(f"ATR {atr_pct:.2f}% too high")

    patterns = c.get("patterns") or []
    pattern_names = " ".join(
        str(p.get("name", "")) for p in patterns if isinstance(p, dict)
    ).lower()
    if bool(_rule(params, "entry", "price_action", "require_breakout_or_retest", default=True)):
        if not (c.get("breakout") or "retest" in pattern_names or "breakout" in pattern_names):
            rejections.append("no breakout or retest price action")
    if bool(_rule(params, "entry", "price_action", "require_bullish_candle", default=True)):
        if not c.get("breakout") and not any(
            isinstance(p, dict) and str(p.get("direction", "")).lower() == "bullish"
            for p in patterns
        ):
            rejections.append("no bullish candle confirmation")
    if bool(_rule(params, "entry", "price_action", "use_closed_candle", default=True)):
        if c.get("closed_candle") is not True:
            rejections.append("signal is not on a closed candle")
    quality_info = c.get("quality") or {}
    if bool(_rule(params, "entry", "quality", "require_relative_strength", default=True)):
        if float(quality_info.get("score") or 100) < 50:
            rejections.append("stock relative strength below V4 quality gate")

    atr_abs = price * atr_pct / 100 if atr_pct else price * 0.01
    stop_dist = stop_distance(price, atr_abs, params)
    stop = round(price - stop_dist, 2)
    target_r = float(_rule(params, "exit", "target", "target_r_mult",
                           default=cfg("exit", "target_r_mult", default=2.0)))
    target = round(price + stop_dist * target_r, 2)

    resistance = c.get("resistance")
    if resistance and float(resistance) > price * 1.003:
        capped = round(min(target, float(resistance)), 2)
        if capped > price:
            target = capped

    risk_reward = round((target - price) / (price - stop), 2) if price > stop else 0.0
    min_rr = float(_rule(params, "entry", "risk", "minimum_risk_reward",
                         default=cfg("entry", "min_risk_reward", default=2.0)))
    if risk_reward < min_rr:
        rejections.append(f"risk/reward {risk_reward} below minimum {min_rr:g}")

    qty = size_position(price, stop, equity, cash, params=params) if not rejections else 0
    min_value = float(_p(params, "account", "minimum_order_value", 100))
    if not rejections and price * qty < min_value:
        rejections.append(f"order value below minimum ₹{min_value:g}")
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


def _trailing_stop(entry: float, stop: float, highest: float, initial_risk: float,
                   r_multiple: float, params: dict | None = None) -> float:
    """V4 stepped ATR + profit-lock trail; never moves the stop backwards."""
    new_stop = stop
    activate = float(_rule(params, "exit", "trailing_stop", "activate_at_r",
                           default=cfg("exit", "trail_after_r", default=1.0)))
    if r_multiple < activate:
        return new_stop
    stop_atr = float(cfg("exit", "stop_atr_mult", default=1.2))
    atr_abs = initial_risk / max(stop_atr, 0.1)
    levels = _rule(params, "exit", "trailing_stop", "profit_lock_levels", default=[]) or []
    for raw in sorted(levels, key=lambda item: float(item.get("profit_at_r", 0))):
        if not isinstance(raw, dict) or r_multiple < float(raw.get("profit_at_r", 0)):
            continue
        by_atr = highest - atr_abs * float(raw.get("atr_multiplier", 1.0))
        by_lock = entry + initial_risk * float(raw.get("lock_profit_r", 0.0))
        new_stop = max(new_stop, by_atr, by_lock)
    return new_stop


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

    square_off = str(_rule(params, "exit", "square_off", "time",
                           default=cfg("exit", "square_off", default="15:20")))
    hh, mm = int(square_off[:2]), int(square_off[3:5])
    if now.hour > hh or (now.hour == hh and now.minute >= mm):
        return {"exit": True, "reason": "SQUARE_OFF", "new_stop": stop, "r_multiple": r_multiple}

    if price <= stop:
        reason = "TRAILING_STOP" if stop > entry else "STOP_LOSS"
        return {"exit": True, "reason": reason, "new_stop": stop, "r_multiple": r_multiple}
    if price >= target:
        return {"exit": True, "reason": "TARGET", "new_stop": stop, "r_multiple": r_multiple}

    opened = _parse_ts(pos.get("opened_at"))
    minutes = int(_rule(params, "exit", "time_stop", "maximum_holding_minutes",
                        default=cfg("exit", "time_stop_minutes", default=180)))
    if opened and (now - opened).total_seconds() / 60 >= minutes:
        return {"exit": True, "reason": "TIME_STOP", "new_stop": stop, "r_multiple": r_multiple}

    new_stop = stop
    be_at = float(_rule(params, "exit", "breakeven", "activate_at_r",
                        default=cfg("exit", "move_stop_to_be_at_r", default=1.0)))
    if r_multiple >= be_at:
        new_stop = max(new_stop, entry)
    new_stop = max(new_stop, _trailing_stop(entry, stop, highest, initial_risk,
                                            r_multiple, params))
    new_stop = round(min(new_stop, price), 2)
    return {"exit": False, "reason": None, "new_stop": new_stop, "r_multiple": r_multiple}


def can_open_new(equity: float, params: dict | None = None) -> tuple[bool, str]:
    """Portfolio-level V4 gates: positions, daily loss, trade count, cooldown, cash."""
    acc = ledger.account()
    open_count = len(ledger.positions())
    max_positions = int(_p(params, "account", "max_positions", 2))
    if open_count >= max_positions:
        return False, f"max {max_positions} open positions"

    max_loss = float(_p(params, "account", "max_daily_loss", 300))
    day_start = float(acc.get("day_start_equity") or equity)
    if equity - day_start <= -abs(max_loss):
        return False, f"daily loss limit hit ({equity - day_start:.0f})"

    today = now_ist().date().isoformat()
    trades = ledger.trades(limit=500)
    todays = [t for t in trades
              if str(t.get("opened_at") or "").startswith(today)
              or str(t.get("closed_at") or "").startswith(today)]
    max_trades = int(_p(params, "account", "max_trades_per_day", 4))
    if len(todays) >= max_trades:
        return False, f"max {max_trades} trades per day reached"

    closed = sorted(
        (t for t in trades if str(t.get("closed_at") or "").startswith(today)),
        key=lambda t: str(t.get("closed_at") or ""),
    )
    streak, last_loss = 0, None
    for trade in closed:
        if float(trade.get("pnl") or 0) < 0:
            streak += 1
            last_loss = trade
        else:
            streak = 0
    max_streak = int(_p(params, "account", "max_consecutive_losses", 3))
    stop_after = bool(_rule(params, "risk", "consecutive_losses",
                            "stop_trading_after_limit", default=True))
    if stop_after and streak >= max_streak:
        return False, f"{streak} consecutive losses — V4 trading halt"

    cooldown = float(_p(params, "account", "cooldown_after_loss_minutes", 15))
    loss_at = _parse_ts(str(last_loss.get("closed_at"))) if last_loss else None
    if loss_at and (now_ist() - loss_at).total_seconds() / 60 < cooldown:
        return False, f"cooldown active for {cooldown:g} min after a loss"

    min_cash = float(_p(params, "account", "minimum_order_value", 100))
    leverage = max(float(_p(params, "account", "intraday_leverage", 5)), 1.0)
    if float(acc.get("cash", 0)) * leverage < min_cash:
        return False, "insufficient intraday margin"
    return True, ""
