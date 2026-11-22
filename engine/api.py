"""FastAPI surface for the dashboard."""
from __future__ import annotations

import datetime as dt
import os
import re
import secrets
import threading
import time
from decimal import Decimal
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine import backtest, broker, groww_auth, groww_data, ledger, marketdata, scanner, strategy, universe
from engine.engine import ENGINE
from engine.intelligence.api import router as intelligence_router
from engine.settings import (
    BASE_DIR, CACHE_DIR, CONFIG_DIR, CONFIG_PATH,
    cfg, load_config, load_raw_config, normalize_config, now_ist,
)

app = FastAPI(title="Groww Algo", version="1.0.0",
              description="Intraday market scanner + automatic paper trader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Additive intelligence surface. Existing endpoints and response shapes are unchanged.
app.include_router(intelligence_router)
_INTELLIGENCE_SCHEDULER = None


def _sync_intelligence_scheduler() -> dict | None:
    """Start or stop the research thread to match the live AI/research config.

    This is called both on app startup and immediately after the dashboard mode
    switch, so enabling AI without restarting Render still starts 24x7 research.
    Any failure is contained; the paper-trading engine is unaffected.
    """
    global _INTELLIGENCE_SCHEDULER
    try:
        from engine.intelligence import config as ai_config
        from engine.intelligence import storage as ai_storage

        if not ai_config.enabled() or not ai_config.evolution().get("research_enabled"):
            existing = _INTELLIGENCE_SCHEDULER
            _INTELLIGENCE_SCHEDULER = None
            if existing is not None:
                existing.stop()
            return None

        from engine.intelligence.scheduler import IntelligenceScheduler

        ai_storage.init_db()
        if _INTELLIGENCE_SCHEDULER is None:
            _INTELLIGENCE_SCHEDULER = IntelligenceScheduler()
        return _INTELLIGENCE_SCHEDULER.start()
    except Exception:
        _INTELLIGENCE_SCHEDULER = None
        return None


class ScanRequest(BaseModel):
    autotrade: bool = False


class StrategySave(BaseModel):
    yaml: str


class CredentialsSave(BaseModel):
    api_key: str | None = None
    api_secret: str | None = None
    totp_secret: str | None = None


class ConfigValue(BaseModel):
    section: str
    key: str
    value: float | int | bool | str


class IntelligenceModeRequest(BaseModel):
    enabled: bool


class ResetRequest(BaseModel):
    challenge_id: str
    answer: str
    scope: str = "history"


class BacktestRequest(BaseModel):
    days: int = 22
    preset: str | None = None
    overrides: dict | None = None
    limit: int = 60


class ChartRequest(BaseModel):
    symbol: str
    interval: str = "5m"
    days: int = 5
    row: dict | None = None


@app.on_event("startup")
def _startup() -> None:
    global _INTELLIGENCE_SCHEDULER
    ledger.init_db()
    ledger.ensure_day()
    if cfg("engine", "enabled", default=True):
        ENGINE.start()

    # Optional research scheduler. Any failure is contained so the existing
    # paper-trading app still starts; model/agent health remains fail-closed.
    _sync_intelligence_scheduler()


@app.on_event("shutdown")
def _shutdown() -> None:
    ENGINE.stop()
    try:
        if _INTELLIGENCE_SCHEDULER is not None:
            _INTELLIGENCE_SCHEDULER.stop()
    except Exception:
        pass


# --------------------------------------------------------------------------
# status / overview
# --------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "ts": now_ist().isoformat(timespec="seconds")}


@app.get("/api/status")
def status() -> dict:
    info = ENGINE.status()
    info["config"] = {
        "mode": cfg("mode", default="paper"),
        "version": str(cfg("app", "version", default="")),
        "strategy": str(cfg("app", "name", default="Groww Algo")),
        "execution_mode": str(cfg("engine", "execution_mode", default="paper")),
        "capital": cfg("account", "starting_capital", default=10000),
        "leverage": cfg("account", "intraday_leverage", default=5),
        "risk_per_trade": cfg("account", "risk_per_trade", default=100),
        "max_positions": cfg("account", "max_positions", default=2),
        "max_daily_loss": cfg("account", "max_daily_loss", default=300),
        "min_score": cfg("entry", "min_score", default=65),
        "required_confirmations": cfg("entry", "min_confirmations", default=3),
        "target_r_mult": cfg("exit", "target_r_mult", default=2.0),
        "scan_interval_sec": cfg("engine", "scan_interval_sec", default=300),
        "autotrade": bool(cfg("engine", "autotrade", default=True)),
        "data_source": marketdata.preference(),
        "universe_size": len(universe.load_symbols()),
    }
    info["feed"] = marketdata.health()
    return info


@app.get("/api/overview")
def overview() -> dict:
    return ledger.overview()


@app.get("/api/calendar")
def calendar(year: int = Query(default=None), month: int = Query(default=None)) -> dict:
    now = now_ist()
    year = year or now.year
    month = month or now.month
    if not (1 <= month <= 12):
        raise HTTPException(status_code=400, detail="month must be 1-12")
    days = ledger.calendar_month(year, month)
    return {
        "year": year,
        "month": month,
        "days": days,
        "month_pnl": float(sum((Decimal(str(d.get("pnl", 0) or 0)) for d in days), Decimal(0))),
        "sessions": len(days),
        "green": sum(1 for d in days if d.get("pnl", 0) > 0),
        "red": sum(1 for d in days if d.get("pnl", 0) < 0),
        "demo": ledger.is_demo(),
    }


@app.get("/api/equity")
def equity(limit: int = 400) -> dict:
    curve = ledger.equity_curve(limit)
    return {"curve": curve, "demo": ledger.is_demo()}


# --------------------------------------------------------------------------
# scanner
# --------------------------------------------------------------------------
@app.get("/api/scan")
def latest_scan() -> dict:
    cap = int(cfg("scan", "max_results", default=200))
    if ENGINE.last_scan:
        data = ENGINE.last_scan
        return {
            "ts": data.get("ts"), "source": data.get("source"),
            "scanned": data.get("scanned"), "eligible": data.get("eligible"),
            "qualified": data.get("qualified"), "watchlist": data.get("watchlist"),
            "duration_ms": data.get("duration_ms"),
            "results": (data.get("results") or [])[:cap],
            "signals": data.get("signals", []),
            "trade_errors": data.get("trade_errors", []),
            "errors": data.get("errors", []),
        }
    stored = ledger.latest_scan()
    run = stored.get("run") or {}
    return {
        "ts": run.get("ts"), "source": run.get("source"),
        "scanned": run.get("scanned"), "qualified": run.get("qualified"),
        "watchlist": run.get("watchlist"), "duration_ms": run.get("duration_ms"),
        "results": [], "signals": stored.get("signals", []),
        "trade_errors": [], "errors": [],
    }


@app.post("/api/scan")
def run_scan(req: ScanRequest) -> dict:
    cap = int(cfg("scan", "max_results", default=200))
    result = ENGINE.scan_now(autotrade=req.autotrade)
    return {
        "ts": result.get("ts"), "source": result.get("source"),
        "scanned": result.get("scanned"), "eligible": result.get("eligible"),
        "qualified": result.get("qualified"), "watchlist": result.get("watchlist"),
        "duration_ms": result.get("duration_ms"),
        "results": (result.get("results") or [])[:cap],
        "signals": result.get("signals", []),
        "trade_errors": result.get("trade_errors", []),
        "errors": result.get("errors", []),
    }


@app.get("/api/signals")
def signals(day: str | None = None) -> dict:
    day = day or now_ist().date().isoformat()
    return {"day": day, "signals": ledger.signals_for_day(day)}


# --------------------------------------------------------------------------
# portfolio
# --------------------------------------------------------------------------
@app.get("/api/positions")
def positions() -> dict:
    rows = ledger.positions()
    price_map = marketdata.prices([p["symbol"] for p in rows]) if rows else {}
    if price_map:
        broker.mark(price_map)
        rows = ledger.positions()
    acc = ledger.account()
    return {
        "positions": rows,
        "cash": round(float(acc.get("cash", 0)), 2),
        "equity": ledger.current_equity(),
    }


@app.get("/api/trades")
def trades(limit: int = 200, symbol: str | None = None) -> dict:
    return {"trades": ledger.trades(limit=limit, symbol=symbol)}


# --------------------------------------------------------------------------
# single-request real-time snapshot (dashboard polls this every second)
# --------------------------------------------------------------------------
@app.get("/api/live")
def live(year: int | None = None, month: int | None = None) -> dict:
    """Return one consistent snapshot for the dashboard.

    Open positions are re-marked with the latest available prices first, then
    today's equity is snapshotted. That makes the Overview P&L and the calendar
    cell for today show exactly the same number on every tick.
    """
    now = now_ist()
    year = year or now.year
    month = month or now.month
    pos = positions()  # refreshes marks from the live feed
    ledger.snapshot_equity()
    return {
        "ts": now_ist().isoformat(timespec="seconds"),
        "range": {"year": year, "month": month},
        "status": status(),
        "overview": ledger.overview(),
        "calendar": calendar(year, month),
        "curve": ledger.equity_curve(400),
        "positions": pos,
        "trades": ledger.trades(limit=300),
        "scan": latest_scan(),
    }


# --------------------------------------------------------------------------
# strategy chart: candles + the active strategy rules drawn on them
# --------------------------------------------------------------------------
_CHART_INTERVALS = {"1m", "2m", "3m", "5m", "10m", "15m", "30m", "60m", "1h", "4h", "1d", "1wk"}


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(value * alpha + out[-1] * (1 - alpha))
    return out


def _vwap_series(candles: list[dict]) -> list[float]:
    """Session cumulative VWAP; the day resets on the first bar of each date."""
    out: list[float] = []
    day = None
    pv = volume = 0.0
    for bar in candles:
        bar_day = str(bar["t"])[:10]
        if bar_day != day:
            day, pv, volume = bar_day, 0.0, 0.0
        weight = float(bar.get("v") or 0) or 1.0
        pv += ((float(bar["h"]) + float(bar["l"]) + float(bar["c"])) / 3) * weight
        volume += weight
        out.append(pv / volume if volume else float(bar["c"]))
    return out


def _atr_from_candles(candles: list[dict], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        high, low, prev = (float(candles[i]["h"]), float(candles[i]["l"]),
                           float(candles[i - 1]["c"]))
        trs.append(max(high - low, abs(high - prev), abs(low - prev)))
    window = trs[-period:]
    return sum(window) / len(window) if window else 0.0


def _fetch_chart_candles(symbol: str, interval: str, days: int) -> tuple[list[dict], str, bool, str | None]:
    """Groww first; Yahoo keeps the chart usable while approval is pending."""
    error = None
    rows: list[dict] = []
    if marketdata.groww_usable():
        try:
            rows = list(groww_data.candles(symbol, interval, days).get("candles") or [])
        except Exception as exc:  # noqa: BLE001 - feed errors must not break the chart
            error = str(exc)[:200]
    if rows:
        return rows, "groww", False, None

    try:
        from engine import yahoo

        if interval == "1d":
            frame = (yahoo.daily([symbol], "1y") or {}).get(symbol)
        else:
            frame = (yahoo.intraday([symbol], interval, f"{days}d") or {}).get(symbol)
        if frame is not None and len(frame):
            rows = [
                {"t": str(ts), "o": float(r["o"]), "h": float(r["h"]),
                 "l": float(r["l"]), "c": float(r["c"]), "v": int(r["v"] or 0)}
                for ts, r in frame.iterrows()
            ][-400:]
            return rows, "yahoo", True, None
    except Exception as exc:  # noqa: BLE001
        error = error or str(exc)[:200]
    return [], "none", False, error


@app.post("/api/chart")
def strategy_chart(req: ChartRequest) -> dict:
    """Candles with entry/stop/target and a plain-language strategy checklist."""
    symbol = re.sub(r"[^A-Za-z0-9.&_-]", "", req.symbol).upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    interval = req.interval if req.interval in _CHART_INTERVALS else "5m"
    days = max(1, min(int(req.days or 5), 60))
    row = req.row or {}

    candles, source, delayed, error = _fetch_chart_candles(symbol, interval, days)
    closes = [float(c["c"]) for c in candles]
    price = float(row.get("price") or (closes[-1] if closes else 0) or 0)
    ind = row.get("indicators") or {}
    vwap = float(ind.get("vwap") or 0)
    ema20 = float(ind.get("ema20") or 0)
    ema50 = float(ind.get("ema50") or 0)
    rel_vol = float(ind.get("rel_vol") or 0)
    atr_pct = float(ind.get("atr_pct") or 0)
    ema5_20 = float(ind.get("ema20_5m") or 0)
    ema5_50 = float(ind.get("ema50_5m") or 0)
    ema15_20 = float(ind.get("ema20_15m") or 0)
    ema15_50 = float(ind.get("ema50_15m") or 0)
    ema15_200 = float(ind.get("ema200_15m") or 0)
    trend_conf = row.get("trend_confirmation") or {}
    atr = float(row.get("atr") or 0) or (price * atr_pct / 100 if atr_pct else 0)
    if not atr and candles:
        atr = _atr_from_candles(candles)

    stop_dist = strategy.stop_distance(price, atr) if price else 0.0
    stop = price - stop_dist
    target_r = float(cfg("exit", "target", "target_r_mult",
                         default=cfg("exit", "target_r_mult", default=2.0)))
    target = price + stop_dist * target_r
    resistance = row.get("resistance")
    if resistance and float(resistance) > price * 1.003 and float(resistance) < target:
        target = float(resistance)
    support = row.get("support")
    risk_reward = (target - price) / stop_dist if stop_dist else 0.0

    def rule(label: str, required: str, actual: str, ok: bool | None) -> dict:
        return {"label": label, "required": required, "actual": actual, "ok": ok}

    score = float(row.get("score") or 0)
    confirmations = int(row.get("confirmations") or 0)
    min_score = float(cfg("entry", "minimum_score", default=cfg("entry", "min_score", default=85)))
    min_conf = int(cfg("entry", "required_confirmations",
                       default=cfg("entry", "min_confirmations", default=5)))
    require_trend = bool(cfg("entry", "trend", "ema_alignment",
                             default=cfg("entry", "require_trend", default=True)))
    require_vwap = bool(cfg("entry", "trend", "price_above_vwap",
                            default=cfg("entry", "require_above_vwap", default=True)))
    rsi_min = float(cfg("entry", "momentum", "rsi_min", default=55))
    rsi_max = float(cfg("entry", "momentum", "rsi_max", default=72))
    adx_min = float(cfg("entry", "momentum", "adx_min", default=20))
    rsi = float(ind.get("rsi") or 0)
    adx = float(ind.get("adx") or 0)
    ema200 = float(ind.get("ema200") or 0)
    min_rv = float(cfg("entry", "volume", "relative_volume_min",
                       default=cfg("entry", "min_relative_volume", default=1.5)))
    min_atr = float(cfg("entry", "volatility", "minimum_atr_pct",
                        default=cfg("entry", "min_atr_pct", default=0.4)))
    max_atr = float(cfg("entry", "volatility", "maximum_atr_pct",
                        default=cfg("entry", "max_atr_pct", default=3.5)))
    max_ext = float(cfg("entry", "max_vwap_extension_pct", default=2.5))
    min_rr = float(cfg("entry", "risk", "minimum_risk_reward",
                       default=cfg("entry", "min_risk_reward", default=2.0)))
    version = str(cfg("app", "version", default=""))
    closed_only = bool(cfg("engine", "use_closed_candles_only", default=True))

    rules: list[dict] = []
    if row:
        rules.append(rule("Composite score (V4)", f">= {min_score:g}",
                          f"{score:g}", score >= min_score))
        rules.append(rule("Mandatory confirmations", f">= {min_conf} of 5",
                          str(confirmations), confirmations >= min_conf))
        status = str(row.get("status") or "").upper()
        rules.append(rule("Scanner status", "QUALIFIED", status or "—", status == "QUALIFIED" or None))
        if require_trend:
            aligned = bool(ema20 and ema50 and ema200 and ema20 > ema50 > ema200
                           and price > ema20)
            rules.append(rule("Daily EMA stack", "EMA20 > EMA50 > EMA200",
                              f"{ema20:.2f} / {ema50:.2f} / {ema200:.2f}"
                              if ema20 and ema50 and ema200 else "—", aligned))
            rules.append(rule("5m EMA confirmation", "price > EMA20 > EMA50",
                              f"{ema5_20:.2f} / {ema5_50:.2f}"
                              if ema5_20 and ema5_50 else "—",
                              trend_conf.get("5m")))
            rules.append(rule("15m EMA confirmation", "price > EMA20 > EMA50 > EMA200",
                              f"{ema15_20:.2f} / {ema15_50:.2f} / {ema15_200:.2f}"
                              if ema15_20 and ema15_50 else "—",
                              trend_conf.get("15m")))
        else:
            rules.append(rule("Trend filter", "disabled in strategy.yaml", "not required", None))
        if require_vwap:
            extension = (price / vwap - 1) * 100 if vwap else 0
            rules.append(rule("Price above VWAP", f"extension <= {max_ext:g}%",
                              f"{extension:.2f}%" if vwap else "—",
                              bool(vwap and price >= vwap and extension <= max_ext)))
        else:
            rules.append(rule("VWAP filter", "disabled in strategy.yaml", "not required", None))
        rules.append(rule("RSI band", f"{rsi_min:g} - {rsi_max:g}",
                          f"{rsi:.1f}" if rsi else "—",
                          rsi_min <= rsi <= rsi_max if rsi else None))
        rules.append(rule("ADX trend strength", f">= {adx_min:g}",
                          f"{adx:.1f}" if adx else "—", adx >= adx_min if adx else None))
        rules.append(rule("Relative volume", f">= {min_rv:g}x",
                          f"{rel_vol:.2f}x" if rel_vol else "—", rel_vol >= min_rv if rel_vol else None))
        rules.append(rule("ATR volatility", f"{min_atr:g}% - {max_atr:g}%",
                          f"{atr_pct:.2f}%" if atr_pct else "—",
                          min_atr <= atr_pct <= max_atr if atr_pct else None))
        patterns = ", ".join(str(p.get("name")) for p in (row.get("patterns") or [])
                             if isinstance(p, dict)) or "none detected"
        rules.append(rule("Price pattern / breakout", "breakout, retest or candle pattern",
                          patterns, bool(row.get("patterns")) or None))
        rules.append(rule("Closed candle", "closed bars only" if closed_only else "forming bars allowed",
                          "closed 5m bar" if row.get("closed_candle", True) else "forming bar",
                          bool(row.get("closed_candle", True)) if closed_only else None))
        rules.append(rule("Risk : reward", f">= {min_rr:g}R",
                          f"{risk_reward:.2f}R", risk_reward >= min_rr))
    else:
        rules.append(rule("Select a scanner row", "—", "no candidate selected", None))

    stop_mult = float(cfg("exit", "initial_stop_loss", "atr_multiplier",
                          default=cfg("exit", "stop_atr_mult", default=1.2)))
    min_stop = float(cfg("exit", "initial_stop_loss", "minimum_stop_pct",
                         default=cfg("exit", "min_stop_pct", default=0.4)))
    max_stop = float(cfg("exit", "initial_stop_loss", "maximum_stop_pct",
                         default=cfg("exit", "max_stop_pct", default=2.0)))
    rules.append(rule("Stop loss", f"ATR x {stop_mult:g}, clamped {min_stop:g}%-{max_stop:g}%",
                      f"₹{stop:.2f} ({stop_dist:.2f} below entry)", True))
    rules.append(rule("Target", f"{target_r:g}R" + (" (capped at resistance)" if resistance and float(resistance) < price + stop_dist * target_r else ""),
                      f"₹{target:.2f}", True))
    rules.append(rule("Time stop / square-off",
                      f"{int(cfg('exit', 'time_stop', 'maximum_holding_minutes', default=cfg('exit', 'time_stop_minutes', default=180)))} min, "
                      f"{cfg('exit', 'square_off', 'time', default=cfg('exit', 'square_off', default='15:20'))} IST",
                      "intraday only", True))
    health = marketdata.health()
    if bool(cfg("data", "groww", "use_for_entry_confirmation", default=True)):
        rules.append(rule("Groww entry confirmation", "fresh Groww quote",
                          str(health.get("state", "unknown")), bool(marketdata.groww_usable())))
    else:
        rules.append(rule("Entry confirmation", "disabled in strategy.yaml", "not required", None))
    if version:
        rules.append(rule("Strategy document", f"V{version} · {str(cfg('app', 'mode', default='paper')).upper()}",
                          str(cfg("app", "name", default="Groww Algo")), True))

    markers = []
    lookback = int(cfg("entry", "breakout_lookback", default=20))
    breakout_i = None
    for i in range(lookback, len(candles)):
        if float(candles[i]["c"]) > max(float(c["h"]) for c in candles[i - lookback:i]):
            breakout_i = i
            break
    for pattern in (row.get("patterns") or []):
        name = pattern.get("name") if isinstance(pattern, dict) else str(pattern)
        direction = pattern.get("direction", "neutral") if isinstance(pattern, dict) else "neutral"
        note = pattern.get("note", "") if isinstance(pattern, dict) else ""
        idx = breakout_i if breakout_i is not None and str(name).lower() == "breakout" else (len(candles) - 1)
        markers.append({"i": max(0, idx or 0), "label": str(name), "direction": str(direction), "note": str(note)})

    last = candles[-1] if candles else None
    return {
        "symbol": symbol,
        "interval": interval,
        "days": days,
        "source": source,
        "delayed": delayed,
        "as_of": str(last["t"]) if last else now_ist().isoformat(timespec="seconds"),
        "count": len(candles),
        "error": error,
        "candles": candles,
        "overlays": {
            "vwap": _vwap_series(candles),
            "ema20": _ema_series(closes, 20),
            "ema50": _ema_series(closes, 50),
        },
        "levels": {
            "entry": price, "stop": stop, "target": target,
            "support": float(support) if support else None,
            "resistance": float(resistance) if resistance else None,
            "vwap": vwap or None, "ema20": ema20 or None, "ema50": ema50 or None,
            "atr": atr, "risk": stop_dist, "reward": target - price, "risk_reward": risk_reward,
        },
        "rules": rules,
        "markers": markers,
    }


@app.post("/api/positions/{trade_id}/close")
def close_position(trade_id: int) -> dict:
    rows = {p["trade_id"]: p for p in ledger.positions()}
    pos = rows.get(trade_id)
    if not pos:
        raise HTTPException(status_code=404, detail="position not found")
    price_map = marketdata.prices([pos["symbol"]])
    price = price_map.get(pos["symbol"]) or float(pos["last_price"] or pos["avg_price"])
    order = broker.sell(trade_id, float(price), "MANUAL")
    if not order.get("ok"):
        raise HTTPException(status_code=400, detail=order.get("error"))
    ledger.snapshot_equity()
    return order


# --------------------------------------------------------------------------
# engine controls
# --------------------------------------------------------------------------
@app.post("/api/engine/start")
def engine_start() -> dict:
    return ENGINE.start()


@app.post("/api/engine/stop")
def engine_stop() -> dict:
    return ENGINE.stop()


@app.post("/api/engine/cycle")
def engine_cycle() -> dict:
    return ENGINE.cycle()


# --------------------------------------------------------------------------
# config / universe
# --------------------------------------------------------------------------
@app.get("/api/config")
def config() -> dict:
    return {
        "config": load_config(force=True),
        "yaml": CONFIG_PATH.read_text(encoding="utf-8"),
        "path": str(CONFIG_PATH),
    }


REQUIRED_SECTIONS_V2 = ("engine", "account", "costs", "universe", "entry", "exit", "scoring")
REQUIRED_SECTIONS_V4 = (
    "app", "engine", "data", "account", "costs", "universe", "indicators",
    "market_filter", "entry", "short_entry", "scoring", "exit", "risk",
    "execution", "performance", "safety",
)


def _nested(data: dict, *path: str, default=None):
    node = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _validate_number(data: dict, path: tuple[str, ...], low: float | None,
                     high: float | None) -> None:
    value = _nested(data, *path)
    if value is None:
        return
    label = ".".join(path)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{label} must be a number") from exc
    if low is not None and number < low:
        raise HTTPException(status_code=400, detail=f"{label} must be >= {low:g}")
    if high is not None and number > high:
        raise HTTPException(status_code=400, detail=f"{label} must be <= {high:g}")


def _validate_weights(data: dict) -> None:
    weights = _nested(data, "scoring", "weights", default={}) or {}
    if not isinstance(weights, dict) or not weights:
        return
    try:
        total = sum(float(v) for v in weights.values())
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="scoring.weights must all be numbers") from exc
    if abs(total - 100) > 0.01:
        raise HTTPException(status_code=400,
                            detail=f"scoring.weights must total 100 (currently {total:g})")


def _validate_time(value, label: str) -> None:
    try:
        hh, mm = str(value).split(":")[:2]
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{label} must be HH:MM") from exc


def _validate_intelligence_config(data: dict) -> None:
    """Block credential redirection through the writable strategy editor."""
    block = data.get("intelligence")
    if isinstance(block, dict):
        for key in ("enabled", "require_for_entries", "fail_closed", "paper_only"):
            if key in block and not isinstance(block[key], bool):
                raise HTTPException(status_code=400, detail=f"intelligence.{key} must be a boolean")
    model = block.get("model") if isinstance(block, dict) else None
    if not isinstance(model, dict):
        return
    try:
        from engine.intelligence.config import validate_model_settings
    except Exception:
        return
    errors = validate_model_settings(model)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))


def _validate_strategy(text: str) -> dict:
    """Parse + sanity-check a V4 or legacy strategy document before writing."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"YAML parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="The strategy must be a YAML mapping")
    _validate_intelligence_config(data)

    is_v4 = bool(data.get("app")) or "minimum_score" in (data.get("entry") or {}) \
        if isinstance(data.get("entry"), dict) else bool(data.get("app"))
    required = REQUIRED_SECTIONS_V4 if is_v4 else REQUIRED_SECTIONS_V2
    missing = [s for s in required if not isinstance(data.get(s), dict)]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing or invalid sections: {', '.join(missing)}")

    if is_v4:
        checks: list[tuple[tuple[str, ...], float | None, float | None]] = [
            (("engine", "scan_interval_seconds"), 15, None),
            (("engine", "quote_refresh_seconds"), 1, None),
            (("account", "initial_capital"), 1, None),
            (("account", "risk_per_trade"), 0.01, None),
            (("account", "max_positions"), 1, 20),
            (("account", "max_daily_loss"), 0, None),
            (("account", "max_position_pct"), 1, 100),
            (("account", "intraday_leverage"), 1, 10),
            (("entry", "minimum_score"), 0, 100),
            (("entry", "required_confirmations"), 0, 10),
            (("entry", "min_risk_reward"), 0, None),
            (("entry", "momentum", "rsi_min"), 0, 100),
            (("entry", "momentum", "rsi_max"), 0, 100),
            (("entry", "momentum", "adx_min"), 0, 100),
            (("short_entry", "minimum_score"), 0, 100),
            (("short_entry", "required_confirmations"), 0, 10),
            (("exit", "initial_stop_loss", "atr_multiplier"), 0.1, None),
            (("exit", "initial_stop_loss", "minimum_stop_pct"), 0, None),
            (("exit", "initial_stop_loss", "maximum_stop_pct"), 0, None),
            (("exit", "target", "target_r_mult"), 0.1, None),
            (("exit", "time_stop", "maximum_holding_minutes"), 1, None),
            (("costs", "brokerage", "per_order"), 0, None),
            (("costs", "brokerage", "percentage"), 0, 100),
            (("costs", "gst_pct"), 0, 100),
            (("costs", "slippage_pct"), 0, 100),
            (("data", "validation", "max_quote_age_seconds"), 0, None),
        ]
        for path, low, high in checks:
            _validate_number(data, path, low, high)
        _validate_weights(data)
        _validate_time(_nested(data, "exit", "square_off", "time", default="15:20"),
                       "exit.square_off.time")

        source = str(_nested(data, "data", "source_mode", default="auto")).strip().lower()
        if source == "yahoo_finance":
            source = "yahoo"
        if source not in ("auto", "groww", "yahoo"):
            raise HTTPException(status_code=400,
                                detail="data.source_mode must be one of: auto, groww, yahoo_finance")

        if str(_nested(data, "app", "mode", default="paper")).lower() != "paper":
            raise HTTPException(status_code=400, detail="V4 only supports app.mode: paper")
        if str(_nested(data, "engine", "execution_mode", default="paper")).lower() != "paper":
            raise HTTPException(status_code=400, detail="engine.execution_mode must be paper")
        if _nested(data, "execution", "live_trading", "allow_live_orders", default=False):
            raise HTTPException(status_code=400, detail="execution.live_trading must stay disabled")
        for key in ("live_trading_disabled", "real_order_placement_disabled"):
            if _nested(data, "safety", key, default=True) is False:
                raise HTTPException(status_code=400, detail=f"safety.{key} must be true")
        return data

    legacy_checks: list[tuple[tuple[str, ...], float | None, float | None]] = [
        (("account", "starting_capital"), 1, None),
        (("account", "risk_per_trade"), 0.01, None),
        (("account", "max_positions"), 1, 20),
        (("account", "max_daily_loss"), 0, None),
        (("account", "max_position_pct"), 1, 100),
        (("entry", "min_score"), 0, 100),
        (("entry", "min_confirmations"), 0, 6),
        (("entry", "min_relative_volume"), 0, None),
        (("entry", "min_risk_reward"), 0, None),
        (("exit", "target_r_mult"), 0.1, None),
        (("exit", "time_stop_minutes"), 1, None),
        (("engine", "scan_interval_sec"), 15, None),
        (("costs", "brokerage_per_order"), 0, None),
        (("costs", "brokerage_pct"), 0, 100),
        (("costs", "gst_pct"), 0, 100),
        (("costs", "slippage_pct"), 0, 100),
    ]
    for path, low, high in legacy_checks:
        _validate_number(data, path, low, high)
    _validate_weights(data)
    _validate_time(_nested(data, "exit", "square_off", default="15:20"), "exit.square_off")

    source = str(_nested(data, "engine", "data_source", default="auto")).strip().lower()
    if source not in ("auto", "groww", "yahoo"):
        raise HTTPException(status_code=400,
                            detail="engine.data_source must be one of: auto, groww, yahoo")
    return data


def _write_strategy(text: str) -> None:
    backup = CONFIG_PATH.with_suffix(".yaml.bak")
    if CONFIG_PATH.exists():
        backup.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


@app.put("/api/config")
def config_save(req: StrategySave) -> dict:
    _validate_strategy(req.yaml)
    _write_strategy(req.yaml)
    return {
        "ok": True,
        "config": load_config(force=True),
        "yaml": CONFIG_PATH.read_text(encoding="utf-8"),
        "message": "Saved. The next scan uses the new rules — no restart needed.",
    }


@app.post("/api/config/default")
def config_default() -> dict:
    default_path = CONFIG_DIR / "strategy.default.yaml"
    if not default_path.exists():
        raise HTTPException(status_code=404, detail="strategy.default.yaml is missing")
    text = default_path.read_text(encoding="utf-8")
    _validate_strategy(text)
    _write_strategy(text)
    return {
        "ok": True,
        "config": load_config(force=True),
        "yaml": CONFIG_PATH.read_text(encoding="utf-8"),
        "message": "Default strategy restored.",
    }


def _format_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        number = float(value)
        return str(int(number)) if number.is_integer() else f"{number:g}"
    return f'"{value}"'


def _set_scalar(text: str, section: str, key: str, value) -> str:
    """Replace one scalar in-place, preserving indentation and inline comments."""
    lines = text.splitlines()
    section_re = re.compile(rf"^{re.escape(section)}\s*:\s*(?:#.*)?$")
    key_re = re.compile(
        rf"^(?P<indent>[ \t]+)(?P<key>{re.escape(key)})(?P<sep>\s*:\s*)"
        rf"(?P<val>[^#]*?)(?P<tail>\s*(?:#.*)?)$"
    )
    in_section = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if line and not line[0].isspace() and stripped and not stripped.startswith("#"):
            in_section = bool(section_re.match(line))
            continue
        if not in_section:
            continue
        match = key_re.match(line)
        if match:
            lines[index] = (
                f"{match.group('indent')}{match.group('key')}{match.group('sep')}"
                f"{_format_scalar(value)}{match.group('tail')}"
            )
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    raise HTTPException(status_code=404, detail=f"{section}.{key} not found in strategy.yaml")


@app.post("/api/config/value")
def config_value(req: ConfigValue) -> dict:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    updated = _set_scalar(text, req.section, req.key, req.value)
    _validate_strategy(updated)
    _write_strategy(updated)
    return {
        "ok": True,
        "config": load_config(force=True),
        "yaml": updated,
        "message": f"{req.section}.{req.key} = {req.value}",
    }


@app.post("/api/intelligence/mode")
def intelligence_mode(req: IntelligenceModeRequest, request: Request) -> dict:
    """Switch between the normal deterministic engine and the AI advisory layer.

    This is a single operator-controlled boolean. It is open when no admin token
    is configured (the same trust boundary as the existing Settings editor), and
    automatically admin-gated once INTELLIGENCE_ADMIN_TOKEN is set. It never
    touches risk limits, execution permissions, model credentials or the paper-only
    flags, and both strategy YAML files are fully validated before writing.
    """
    token = os.environ.get("INTELLIGENCE_ADMIN_TOKEN") or ""
    if token:
        from engine.intelligence.api import _require_admin
        _require_admin(request)

    active_text = _set_scalar(
        CONFIG_PATH.read_text(encoding="utf-8"), "intelligence", "enabled", req.enabled,
    )
    _validate_strategy(active_text)

    default_path = CONFIG_DIR / "strategy.default.yaml"
    default_text = None
    if default_path.exists():
        default_text = _set_scalar(
            default_path.read_text(encoding="utf-8"), "intelligence", "enabled", req.enabled,
        )
        _validate_strategy(default_text)

    _write_strategy(active_text)
    if default_text is not None:
        tmp = default_path.with_suffix(".yaml.tmp")
        tmp.write_text(default_text, encoding="utf-8")
        os.replace(tmp, default_path)

    load_config(force=True)
    scheduler_status = _sync_intelligence_scheduler()
    mode = "AI" if req.enabled else "NORMAL"
    return {
        "ok": True,
        "enabled": bool(req.enabled),
        "mode": mode,
        "scheduler": scheduler_status,
        "config": load_config(force=True),
        "yaml": CONFIG_PATH.read_text(encoding="utf-8"),
        "message": f"{mode} mode active; the next scan uses this setting.",
    }


@app.post("/api/config/parse")
def config_parse(req: StrategySave) -> dict:
    """Validate YAML without saving — used for the live editor preview."""
    raw = _validate_strategy(req.yaml)
    return {"ok": True, "config": normalize_config(raw)}


# --------------------------------------------------------------------------
# backtest
# --------------------------------------------------------------------------
def _merge_overrides(base: dict | None, extra: dict | None) -> dict:
    out: dict = {}
    for section, values in (base or {}).items():
        out[section] = dict(values) if isinstance(values, dict) else values
    for section, values in (extra or {}).items():
        if isinstance(values, dict):
            node = dict(out.get(section) or {})
            node.update(values)
            out[section] = node
        else:
            out[section] = values
    return out


@app.get("/api/backtest/presets")
def backtest_presets() -> dict:
    return {"presets": [{"id": key, **value} for key, value in backtest.PRESETS.items()]}


@app.post("/api/backtest")
def backtest_run(req: BacktestRequest) -> dict:
    overrides: dict | None = req.overrides
    if req.preset and req.preset in backtest.PRESETS:
        overrides = _merge_overrides(backtest.PRESETS[req.preset]["overrides"], req.overrides)
    limit = max(5, min(int(req.limit), 200))
    symbols = universe.load_symbols()[:limit]
    result = backtest.run(days=req.days, overrides=overrides, symbols=symbols)
    result["preset"] = req.preset
    result["limit"] = limit
    return result


# --------------------------------------------------------------------------
# Groww credentials (write-only; secrets are never returned)
# --------------------------------------------------------------------------
@app.get("/api/credentials")
def credentials_status() -> dict:
    status = groww_auth.status()
    health = marketdata.health(force=True)
    return {
        "status": status,
        "health": health,
        "env_path": str(groww_auth.ENV_PATH),
    }


@app.post("/api/credentials")
def credentials_save(req: CredentialsSave) -> dict:
    api_key = (req.api_key or "").strip() or None
    api_secret = (req.api_secret or "").strip() or None
    totp_secret = (req.totp_secret or "").strip() or None
    if not any((api_key, api_secret, totp_secret)):
        raise HTTPException(status_code=400, detail="Nothing to save")
    if api_secret and totp_secret:
        raise HTTPException(status_code=400,
                            detail="Use either the API secret or the TOTP secret, not both")
    groww_auth.save_credentials(api_key=api_key, api_secret=api_secret, totp_secret=totp_secret)
    health = marketdata.health(force=True)
    return {
        "ok": bool(health.get("ok")),
        "status": groww_auth.status(),
        "health": health,
        "message": health.get("message"),
    }


@app.post("/api/credentials/test")
def credentials_test() -> dict:
    groww_auth.invalidate()
    health = marketdata.health(force=True)
    return {"ok": bool(health.get("ok")), "status": groww_auth.status(), "health": health}


@app.get("/api/groww/portfolio")
def groww_portfolio(force: bool = False) -> dict:
    """Live Groww portfolio: demat holdings, intraday positions and orders."""
    auth = groww_auth.status()
    feed = marketdata.health(force=bool(force))
    errors: dict[str, str] = {}

    account = None
    try:
        account = groww_data.user_detail(ttl=0 if force else 300)
    except Exception as exc:  # includes GrowwUnavailable
        errors["account"] = str(exc)

    holdings_raw: list[dict] = []
    try:
        holdings_raw = groww_data.holdings(ttl=0 if force else 60)
    except Exception as exc:
        errors["holdings"] = str(exc)

    positions_raw: list[dict] = []
    try:
        positions_raw = groww_data.positions(ttl=0 if force else 20)
    except Exception as exc:
        errors["positions"] = str(exc)

    orders_raw: list[dict] = []
    try:
        orders_raw = groww_data.orders(ttl=0 if force else 20)
    except Exception as exc:
        errors["orders"] = str(exc)

    symbols = sorted({row["symbol"] for row in holdings_raw}
                     | {row["symbol"] for row in positions_raw})
    price_map: dict[str, dict] = {}
    if symbols:
        try:
            price_map = {q["symbol"]: q for q in marketdata.quotes(symbols)}
        except Exception:
            price_map = {}

    def price_of(symbol: str) -> tuple[float | None, dict]:
        q = price_map.get(symbol)
        if not q:
            return None, {}
        price = q.get("price")
        return (float(price) if price is not None else None), q

    holdings: list[dict] = []
    invested = value = day_pnl_total = 0.0
    missing_prices: list[str] = []
    for row in holdings_raw:
        price, q = price_of(row["symbol"])
        qty = float(row["quantity"])
        avg = float(row["average_price"])
        cost = qty * avg
        invested += cost
        if price is None:
            missing_prices.append(row["symbol"])
            holdings.append({**row, "price": None, "price_source": None,
                             "delayed": True, "value": None, "pnl": None,
                             "pnl_pct": None, "day_change": None, "day_pnl": None,
                             "weight_pct": None})
            continue
        current = qty * price
        value += current
        pnl = current - cost
        change = q.get("change")
        day_change = float(change) if change is not None else None
        day_pnl = qty * day_change if day_change is not None else None
        if day_pnl is not None:
            day_pnl_total += day_pnl
        holdings.append({
            **row,
            "price": round(price, 2),
            "price_source": q.get("source"),
            "delayed": bool(q.get("delayed", True)),
            "value": round(current, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / cost * 100, 2) if cost else None,
            "day_change": round(day_change, 2) if day_change is not None else None,
            "day_pnl": round(day_pnl, 2) if day_pnl is not None else None,
            "weight_pct": None,
        })

    for row in holdings:
        if row.get("value") is not None and value > 0:
            row["weight_pct"] = round(float(row["value"]) / value * 100, 2)

    positions: list[dict] = []
    unrealised_total = 0.0
    for row in positions_raw:
        price, q = price_of(row["symbol"])
        qty = float(row["quantity"])
        net = float(row["net_price"])
        entry = qty * net
        if price is None:
            positions.append({**row, "price": None, "price_source": None,
                              "delayed": True, "value": round(entry, 2),
                              "unrealised_pnl": None, "unrealised_pct": None})
            continue
        current = qty * price
        upnl = current - entry
        unrealised_total += upnl
        positions.append({
            **row,
            "price": round(price, 2),
            "price_source": q.get("source"),
            "delayed": bool(q.get("delayed", True)),
            "value": round(current, 2),
            "unrealised_pnl": round(upnl, 2),
            "unrealised_pct": round(upnl / entry * 100, 2) if entry else None,
        })

    total_pnl = value - invested if value else 0.0
    summary = {
        "holdings_count": len(holdings),
        "positions_count": len(positions),
        "orders_count": len(orders_raw),
        "invested": round(invested, 2),
        "current_value": round(value, 2) if value else 0.0,
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / invested * 100, 2) if invested else 0.0,
        "day_pnl": round(day_pnl_total, 2),
        "unrealised_pnl": round(unrealised_total, 2),
        "missing_prices": missing_prices,
        "priced_by": sorted({q.get("source") for q in price_map.values() if q.get("source")}),
    }

    return {
        "ok": account is not None and not errors.get("holdings"),
        "errors": errors,
        "account": {
            "ucc": (account or {}).get("ucc"),
            "segments": (account or {}).get("active_segments") or [],
            "nse_enabled": bool((account or {}).get("nse_enabled")),
            "bse_enabled": bool((account or {}).get("bse_enabled")),
        } if account else None,
        "holdings": holdings,
        "positions": positions,
        "orders": orders_raw,
        "summary": summary,
        "auth": {
            "state": auth.get("state"),
            "message": auth.get("message"),
            "auth_mode": auth.get("auth_mode"),
            "token_cached": auth.get("token_cached"),
            "token_expiry": auth.get("token_expiry"),
            "needs_daily_approval": auth.get("needs_daily_approval"),
            "api_key_hint": auth.get("api_key_hint"),
        },
        "feed": {
            "preference": feed.get("preference"),
            "active": feed.get("active"),
            "ok": feed.get("ok"),
            "portfolio_ok": feed.get("portfolio_ok"),
            "live_data": feed.get("live_data"),
            "state": feed.get("state"),
            "message": feed.get("message"),
            "fallback_reason": feed.get("fallback_reason"),
            "checked_at": feed.get("checked_at"),
        },
        "as_of": now_ist().isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------
# Full reset, guarded by a one-time typed captcha
# --------------------------------------------------------------------------
_reset_lock = threading.Lock()
_reset_challenges: dict[str, dict] = {}
_RESET_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_RESET_TTL_SEC = 300
_RESET_MAX_ATTEMPTS = 5


@app.post("/api/reset/challenge")
def reset_challenge() -> dict:
    code = "".join(secrets.choice(_RESET_ALPHABET) for _ in range(6))
    challenge_id = secrets.token_urlsafe(18)
    now = time.time()
    with _reset_lock:
        for key in [k for k, v in _reset_challenges.items() if v["expires"] < now]:
            _reset_challenges.pop(key, None)
        _reset_challenges[challenge_id] = {
            "code": f"RESET-{code}",
            "expires": now + _RESET_TTL_SEC,
            "attempts": 0,
        }
    return {
        "challenge_id": challenge_id,
        "prompt": f"RESET-{code}",
        "expires_in": _RESET_TTL_SEC,
    }


def _clear_market_cache() -> int:
    removed = 0
    for path in CACHE_DIR.glob("*.pkl"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    with groww_data._cache_lock:
        groww_data._cache.clear()
    return removed


@app.post("/api/reset")
def reset_everything(req: ResetRequest) -> dict:
    now = time.time()
    with _reset_lock:
        entry = _reset_challenges.get(req.challenge_id)
        if not entry or entry["expires"] < now:
            _reset_challenges.pop(req.challenge_id, None)
            raise HTTPException(status_code=400, detail="Confirmation expired. Start again.")
        if req.answer.strip().upper() != entry["code"]:
            entry["attempts"] += 1
            if entry["attempts"] >= _RESET_MAX_ATTEMPTS:
                _reset_challenges.pop(req.challenge_id, None)
                raise HTTPException(status_code=400, detail="Too many wrong codes. Start again.")
            left = _RESET_MAX_ATTEMPTS - entry["attempts"]
            raise HTTPException(status_code=400, detail=f"Code does not match. {left} tries left.")
        _reset_challenges.pop(req.challenge_id, None)

    scope = req.scope if req.scope in ("history", "factory") else "history"
    if scope == "factory":
        default_path = CONFIG_DIR / "strategy.default.yaml"
        if not default_path.exists():
            raise HTTPException(status_code=404, detail="strategy.default.yaml is missing")
        _write_strategy(default_path.read_text(encoding="utf-8"))
        load_config(force=True)

    capital = float(cfg("account", "starting_capital", default=10000))
    ledger.reset(starting_capital=capital, demo=False)
    removed = _clear_market_cache()
    marketdata.health(force=True)

    ENGINE.last_scan = None
    ENGINE.last_scan_at = None
    ENGINE.next_scan_at = None
    ENGINE.last_error = None

    return {
        "ok": True,
        "scope": scope,
        "starting_capital": capital,
        "cache_files_cleared": removed,
        "message": ("Factory reset complete: history, caches and strategy.yaml are back to defaults."
                    if scope == "factory" else
                    "History and caches cleared. Starting fresh from the configured capital."),
        "status": ENGINE.status(),
        "overview": ledger.overview(),
    }


@app.get("/api/universe")
def universe_list() -> dict:
    return {"symbols": universe.load_symbols()}


# --------------------------------------------------------------------------
# optional static frontend (single-port production mode)
# --------------------------------------------------------------------------
DIST = BASE_DIR / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(DIST / "index.html")

    @app.get("/{path:path}")
    def spa(path: str):
        candidate = DIST / path
        if candidate.exists() and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(DIST / "index.html")
else:
    @app.get("/")
    def root():
        return JSONResponse({
            "name": "Groww Algo API",
            "docs": "/docs",
            "hint": "Run the React app from frontend/ (npm run dev) or build it to frontend/dist.",
        })
