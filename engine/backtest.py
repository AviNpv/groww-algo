"""One-month strategy backtest on daily bars (approximate, no look-ahead).

How it works
------------
For every session in the window the backtester looks at the *previous* close
only, builds the same score the live scanner builds, and — if the setup is
QUALIFIED and passes every entry rule — buys at that session's open. The trade
is then closed inside the same session:

  1. low  <= stop   -> STOP_LOSS  (stop first: conservative when both hit)
  2. high >= target -> TARGET
  3. otherwise      -> SQUARE_OFF at the close (MIS never carries overnight)

Daily bars cannot see intraday order flow, so results are an approximation of
the live 5-minute engine. Costs (brokerage, STT, slippage) and position sizing
use exactly the same code paths as the paper broker.
"""
from __future__ import annotations

import datetime as dt
import threading
from collections import Counter

from engine import broker, indicators, patterns, quality, scoring, strategy, universe, yahoo
from engine.settings import load_config, now_ist

_LOCK = threading.Lock()

# Named starting points for the UI. "live" is literally strategy.yaml, so the
# result matches what the paper engine would do today. The others relax the
# intraday-only gates that daily bars cannot satisfy (VWAP, 5-minute volume
# spikes), which is why the live preset can legitimately produce zero trades.
PRESETS: dict[str, dict] = {
    "live": {
        "label": "Live config",
        "description": "Exactly strategy.yaml. Expect few or zero trades: the live gates are tuned for 5-minute intraday data.",
        "overrides": {},
    },
    "balanced": {
        "label": "Balanced",
        "description": "Trend-following with relaxed score/volume gates so a daily-bar backtest can actually trade.",
        "overrides": {
            "entry": {"min_score": 65, "min_confirmations": 2,
                      "min_relative_volume": 1.0, "min_risk_reward": 1.2},
            "exit": {"target_r_mult": 2.0},
        },
    },
    "aggressive": {
        "label": "Aggressive",
        "description": "More setups, no trend filter, wider targets. Higher trade count, higher variance.",
        "overrides": {
            "entry": {"min_score": 60, "min_confirmations": 2,
                      "min_relative_volume": 0.8, "min_risk_reward": 1.0,
                      "require_trend": False},
            "exit": {"target_r_mult": 2.5},
        },
    },
}


def _merge(base: dict, overrides: dict | None) -> dict:
    out: dict = {}
    for key, value in (base or {}).items():
        out[key] = dict(value) if isinstance(value, dict) else value
    for section, values in (overrides or {}).items():
        if isinstance(values, dict):
            node = dict(out.get(section) or {})
            node.update(values)
            out[section] = node
        else:
            out[section] = values
    return out


def _param(params: dict, section: str, key: str, default):
    node = params.get(section)
    if isinstance(node, dict) and key in node:
        return node[key]
    return default


def run(days: int = 22, overrides: dict | None = None,
        symbols: list[str] | None = None) -> dict:
    """Run one backtest at a time (the scan thread may be using the network)."""
    with _LOCK:
        return _run(int(days), overrides, symbols)


def _run(days: int, overrides: dict | None, symbols: list[str] | None) -> dict:
    days = max(5, min(int(days), 60))
    params = _merge(load_config(force=True), overrides)

    syms = list(symbols or universe.load_symbols())
    cap = int(_param(params, "universe", "max_symbols", 140))
    syms = syms[:max(1, cap)]

    started = dt.datetime.now()
    frames = yahoo.daily(syms, "2y")
    series: dict[str, tuple] = {}
    for sym, frame in frames.items():
        if frame is None or len(frame) < 70:
            continue
        enriched = indicators.enrich(frame)
        if enriched is None or len(enriched) < 70:
            continue
        index = {}
        for position, stamp in enumerate(enriched.index):
            index[stamp.date()] = position
        series[sym] = (enriched, index)

    if not series:
        return {
            "ok": False,
            "error": "No usable daily history came back from the data feed. Try again in a minute.",
            "window": None, "days": [], "trades": [], "summary": None,
        }

    # Trading calendar: every date present in the data, newest `days` sessions.
    all_dates = sorted({day for _, index in series.values() for day in index})
    if len(all_dates) < days + 60:
        days = max(5, len(all_dates) - 60)
    window = all_dates[-days:]
    if not window:
        return {"ok": False, "error": "Not enough history for a backtest.", "window": None,
                "days": [], "trades": [], "summary": None}

    starting_capital = float(_param(params, "account", "starting_capital", 10000))
    max_positions = int(_param(params, "account", "max_positions", 2))
    max_daily_loss = float(_param(params, "account", "max_daily_loss", 200))
    min_score = float(_param(params, "entry", "min_score", 65))
    lookback = int(_param(params, "entry", "breakout_lookback", 20))
    slippage = float(_param(params, "costs", "slippage_pct", 0.05)) / 100.0

    equity = starting_capital
    cash = starting_capital
    trades: list[dict] = []
    day_rows: list[dict] = []
    rejections: Counter[str] = Counter()

    for day in window:
        day_start = equity
        day_trades: list[dict] = []
        opened = 0
        candidates: list[dict] = []

        for sym, (frame, index) in series.items():
            position = index.get(day)
            if position is None or position < 60:
                continue
            history = frame.iloc[:position]          # everything up to yesterday's close
            if len(history) < 40:
                continue
            close = float(history["c"].iloc[-1])
            prev = float(history["c"].iloc[-2]) if len(history) >= 2 else close
            quote = {
                "symbol": sym,
                "price": close,
                "prev_close": prev,
                "change": close - prev,
                "change_pct": (close / prev - 1) * 100 if prev else 0.0,
                "volume": int(history["v"].iloc[-1]),
            }
            ok, _reason = universe.passes_filters(sym, history, quote, params)
            if not ok:
                continue
            try:
                pattern_info = patterns.detect_all(history, None, lookback=lookback)
                quality_info = quality.quality_score(history)
                scored = scoring.score_symbol(
                    sym, quote, history, None, pattern_info, quality_info, params
                )
            except Exception:
                continue
            if float(scored.get("score", 0)) < min_score:
                continue
            candidates.append(scored)

        candidates.sort(key=lambda row: row["score"], reverse=True)

        for candidate in candidates:
            if opened >= max_positions:
                break
            if equity - day_start <= -abs(max_daily_loss):
                break
            signal = strategy.build_entry(candidate, equity, cash, set(), params=params)
            if signal.get("action") != "BUY":
                for reason in (signal.get("rejections") or [])[:1]:
                    rejections[reason] += 1
                continue

            frame, index = series[candidate["symbol"]]
            position = index.get(day)
            row = frame.iloc[position]
            entry = round(float(row["o"]) * (1 + slippage), 2)
            stop = float(signal["stop"])
            target = float(signal["target"])
            if entry <= 0 or entry <= stop or entry >= target:
                rejections["gap through stop/target at the open"] += 1
                continue

            qty = broker.size_position(entry, stop, equity, cash, params=params)
            if qty < 1:
                rejections["position size rounded to zero"] += 1
                continue
            buy_charges = broker.buy_charges(qty, entry, params)
            buy_cost = entry * qty + buy_charges
            if buy_cost > cash:
                rejections["insufficient paper cash"] += 1
                continue

            low = float(row["l"])
            high = float(row["h"])
            close = float(row["c"])
            if low <= stop:
                exit_price, reason = stop, "STOP_LOSS"
            elif high >= target:
                exit_price, reason = target, "TARGET"
            else:
                exit_price, reason = close, "SQUARE_OFF"
            fill = round(exit_price * (1 - slippage), 2)
            sell_charges = broker.sell_charges(qty, fill, params)
            proceeds = fill * qty - sell_charges
            pnl = round(proceeds - buy_cost, 2)

            cash += proceeds - buy_cost
            equity = round(cash, 2)
            opened += 1
            risk_per_share = entry - stop
            trade = {
                "day": day.isoformat(),
                "symbol": candidate["symbol"],
                "qty": qty,
                "entry": round(entry, 2),
                "exit": round(fill, 2),
                "stop": round(stop, 2),
                "target": round(target, 2),
                "pnl": pnl,
                "reason": reason,
                "score": candidate.get("score"),
                "r_multiple": round((fill - entry) / risk_per_share, 2) if risk_per_share > 0 else 0.0,
            }
            trades.append(trade)
            day_trades.append(trade)

        equity = round(cash, 2)
        wins = sum(1 for t in day_trades if t["pnl"] > 0)
        losses = sum(1 for t in day_trades if t["pnl"] < 0)
        day_rows.append({
            "day": day.isoformat(),
            "equity": equity,
            "pnl": round(equity - day_start, 2),
            "trades": len(day_trades),
            "wins": wins,
            "losses": losses,
            "qualified": len(candidates),
        })

    summary = _summarise(starting_capital, equity, trades, day_rows)
    reason_counts = Counter(t["reason"] for t in trades)
    elapsed = (dt.datetime.now() - started).total_seconds()

    return {
        "ok": True,
        "error": None,
        "generated_at": now_ist().isoformat(timespec="seconds"),
        "elapsed_sec": round(elapsed, 1),
        "source": "yahoo daily bars (approximate intraday)",
        "symbols": len(series),
        "window": {
            "start": window[0].isoformat(),
            "end": window[-1].isoformat(),
            "sessions": len(window),
        },
        "params_used": {
            "starting_capital": starting_capital,
            "risk_per_trade": _param(params, "account", "risk_per_trade", 100),
            "max_positions": max_positions,
            "max_daily_loss": max_daily_loss,
            "min_score": min_score,
            "min_confirmations": _param(params, "entry", "min_confirmations", 3),
            "min_relative_volume": _param(params, "entry", "min_relative_volume", 1.4),
            "min_risk_reward": _param(params, "entry", "min_risk_reward", 1.5),
            "target_r_mult": _param(params, "exit", "target_r_mult", 2.0),
            "min_atr_pct": _param(params, "entry", "min_atr_pct", 0.4),
            "max_atr_pct": _param(params, "entry", "max_atr_pct", 3.5),
        },
        "summary": summary,
        "days": day_rows,
        "trades": trades[:400],
        "exit_reasons": dict(reason_counts),
        "rejections": [{"reason": r, "count": c} for r, c in rejections.most_common(8)],
    }


def _summarise(start: float, end: float, trades: list[dict], days: list[dict]) -> dict:
    pnls = [float(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    equities = [start] + [float(d["equity"]) for d in days]
    peak = equities[0]
    max_dd = 0.0
    for value in equities:
        peak = max(peak, value)
        if peak:
            max_dd = max(max_dd, (1 - value / peak) * 100)
    day_pnls = [float(d["pnl"]) for d in days]
    return {
        "starting_capital": round(start, 2),
        "ending_equity": round(end, 2),
        "total_pnl": round(end - start, 2),
        "return_pct": round((end / start - 1) * 100, 2) if start else 0.0,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else (round(gross_profit, 2) if gross_profit else 0.0),
        "avg_win": round(gross_profit / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "best_trade": round(max(pnls), 2) if pnls else 0.0,
        "worst_trade": round(min(pnls), 2) if pnls else 0.0,
        "expectancy": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
        "max_drawdown_pct": round(max_dd, 2),
        "green_days": sum(1 for p in day_pnls if p > 0),
        "red_days": sum(1 for p in day_pnls if p < 0),
        "flat_days": sum(1 for p in day_pnls if p == 0),
        "best_day": round(max(day_pnls), 2) if day_pnls else 0.0,
        "worst_day": round(min(day_pnls), 2) if day_pnls else 0.0,
    }
