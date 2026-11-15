"""Unified market-data layer: Groww real-time prices + bulk candle history.

Design:
  * Quotes  -> Groww when authorised (true real-time), else Yahoo (~15 min delayed).
  * Candles -> Yahoo bulk download (one call for the whole universe).
  * Missing Yahoo history -> per-symbol Groww candles fallback.

The UI always shows which feed produced the data, and why it fell back.
"""
from __future__ import annotations

import datetime as dt
import threading
import time

import pandas as pd

from engine import groww_data, yahoo
from engine.groww_data import GrowwUnavailable
from engine.settings import cfg

_DATA_SOURCES = ("auto", "groww", "yahoo")


def preference() -> str:
    """engine.data_source: auto (Groww first) | groww (never Yahoo) | yahoo."""
    value = str(cfg("engine", "data_source", default="auto")).strip().lower()
    return value if value in _DATA_SOURCES else "auto"


def groww_usable() -> bool:
    """True when Groww market data may be used for this scan."""
    if preference() == "yahoo":
        return False
    return bool(health().get("ok"))


def yahoo_usable() -> bool:
    return preference() != "groww"

_lock = threading.Lock()
_health: dict = {"ts": 0.0, "value": None}
_HEALTH_TTL = 60.0


def _to_frame(rows: list[dict]) -> pd.DataFrame | None:
    if not rows:
        return None
    frame = pd.DataFrame(rows)
    frame["dt"] = pd.to_datetime(frame["t"], errors="coerce", utc=True)
    frame = frame.dropna(subset=["dt"]).set_index("dt")
    try:
        frame.index = frame.index.tz_convert("Asia/Kolkata")
    except (TypeError, AttributeError):
        pass
    frame = frame.rename(columns={"o": "o", "h": "h", "l": "l", "c": "c", "v": "v"})
    return frame[["o", "h", "l", "c", "v"]].astype(float)


def health(force: bool = False) -> dict:
    """Cached Groww health check (never raises)."""
    with _lock:
        fresh = _health["value"] is not None and time.time() - float(_health["ts"]) < _HEALTH_TTL
        if force or not fresh:
            try:
                value = groww_data.health()
            except Exception as exc:  # pragma: no cover - defensive
                value = {"ok": False, "state": "error", "message": str(exc)}
            _health.update(ts=time.time(), value=value)
        info = dict(_health["value"] or {})
    pref = preference()
    info["preference"] = pref
    info["active"] = "groww" if (info.get("ok") and pref != "yahoo") else "yahoo"
    info["fallback_reason"] = None if info.get("ok") else info.get("message")
    info["checked_at"] = dt.datetime.now().isoformat(timespec="seconds")
    info["portfolio_ok"] = bool(info.get("portfolio_ok"))
    info["live_data"] = bool(info.get("ok"))
    if pref == "groww" and not info.get("ok"):
        info["active"] = "none"
        info["fallback_reason"] = (
            "engine.data_source is set to groww, so no fallback feed is allowed. "
            + (info.get("message") or "Groww market data is unavailable."))
    return info


def source() -> str:
    return "groww" if health().get("ok") else "yahoo"


def quotes(symbols: list[str], ttl: float = 8.0) -> list[dict]:
    if not symbols:
        return []
    if groww_usable():
        try:
            live = groww_data.quotes(symbols, ttl=ttl)
            if live:
                return live
        except GrowwUnavailable:
            pass
        except Exception:
            pass
    if not yahoo_usable():
        return []
    try:
        return yahoo.quote_batch(symbols)
    except Exception:
        return []


def quote(symbol: str) -> dict | None:
    rows = quotes([symbol], ttl=4.0)
    return rows[0] if rows else None


def prices(symbols: list[str]) -> dict[str, float]:
    """Cheap last-traded prices for open positions."""
    if not symbols:
        return {}
    if groww_usable():
        try:
            got = groww_data.ltp(symbols)
            out = {}
            for key, value in got.items():
                out[key.split("-")[-1].replace("NSE_", "")] = float(value)
            if out:
                return out
        except Exception:
            pass
    return {q["symbol"]: float(q["price"]) for q in quotes(symbols) if q.get("price")}


def _groww_frames(symbols: list[str], intraday_interval: str) -> dict[str, dict]:
    """Groww-only history — used when engine.data_source is 'groww'."""
    from concurrent.futures import ThreadPoolExecutor

    def one(sym: str):
        daily = intra = None
        try:
            daily = _to_frame(groww_data.candles(sym, "1d", 200)["candles"])
        except Exception:
            daily = None
        try:
            intra = _to_frame(groww_data.candles(sym, intraday_interval, 5)["candles"])
        except Exception:
            intra = None
        return sym, daily, intra

    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for sym, daily, intra in pool.map(one, symbols):
            if daily is None and intra is None:
                continue
            out[sym] = {"daily": daily, "intraday": intra}
    return out


def frames(symbols: list[str], intraday_interval: str = "5m") -> dict[str, dict]:
    """Bulk daily + intraday frames for the whole universe."""
    pref = preference()
    if pref == "groww":
        if not groww_usable():
            return {}
        return _groww_frames(symbols, intraday_interval)

    daily_map = yahoo.daily(symbols, "1y")
    intra_map = yahoo.intraday(symbols, intraday_interval, "5d")

    # Groww fallback for symbols Yahoo could not supply
    missing = [s for s in symbols if s not in daily_map or s not in intra_map]
    if missing and groww_usable():
        for sym in missing:
            try:
                if sym not in daily_map:
                    frame = _to_frame(groww_data.candles(sym, "1d", 200)["candles"])
                    if frame is not None and len(frame):
                        daily_map[sym] = frame
                if sym not in intra_map:
                    frame = _to_frame(groww_data.candles(sym, intraday_interval, 5)["candles"])
                    if frame is not None and len(frame):
                        intra_map[sym] = frame
            except Exception:
                continue

    out: dict[str, dict] = {}
    for sym in symbols:
        daily = daily_map.get(sym)
        intra = intra_map.get(sym)
        if daily is None and intra is None:
            continue
        out[sym] = {"daily": daily, "intraday": intra}
    return out


def snapshot(symbols: list[str], intraday_interval: str = "5m") -> dict[str, dict]:
    """Quotes + frames keyed by symbol, ready for the scanner."""
    quote_rows = {q["symbol"]: q for q in quotes(symbols)}
    frame_map = frames(symbols, intraday_interval)
    out: dict[str, dict] = {}
    for sym in symbols:
        data = frame_map.get(sym)
        if not data:
            continue
        q = quote_rows.get(sym)
        if q is None:
            daily, intra = data.get("daily"), data.get("intraday")
            price = None
            if intra is not None and len(intra):
                price = float(intra["c"].iloc[-1])
            elif daily is not None and len(daily):
                price = float(daily["c"].iloc[-1])
            if price is None:
                continue
            prev = float(daily["c"].iloc[-2]) if daily is not None and len(daily) >= 2 else price
            q = {
                "symbol": sym, "price": round(price, 2), "prev_close": round(prev, 2),
                "change": round(price - prev, 2),
                "change_pct": round((price / prev - 1) * 100, 2) if prev else 0.0,
                "volume": int(intra["v"].sum()) if intra is not None and len(intra) else None,
                "delayed": True, "source": "yahoo",
                "as_of": dt.datetime.now().isoformat(timespec="seconds"),
            }
        out[sym] = {"quote": q, **data}
    return out
