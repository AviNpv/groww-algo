"""Yahoo Finance fallback (free, ~15 min delayed) and fundamentals enrichment.

Groww is the preferred feed. Yahoo keeps the bot working when the Groww
token is missing/expired. Daily bars are cached on disk so a full-universe
scan costs one network call.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import pickle
import threading
import time

import pandas as pd
import yfinance as yf

from engine.settings import CACHE_DIR

_lock = threading.Lock()
_mem: dict[str, tuple[float, object]] = {}

_DAILY_TTL = 4 * 3600
_INTRADAY_TTL = 600
_FUND_TTL = 24 * 3600


def _suffix(symbol: str) -> str:
    s = symbol.strip().upper()
    return s if "." in s else s + ".NS"


def _key(prefix: str, symbols: list[str], *parts) -> str:
    raw = ",".join(sorted(symbols)) + "|" + "|".join(map(str, parts))
    return f"{prefix}_{hashlib.md5(raw.encode()).hexdigest()[:16]}"


def _cache_get(key: str, ttl: float):
    with _lock:
        hit = _mem.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
    path = CACHE_DIR / f"{key}.pkl"
    try:
        if path.exists() and time.time() - path.stat().st_mtime < ttl:
            with path.open("rb") as fh:
                value = pickle.load(fh)
            with _lock:
                _mem[key] = (time.time() + ttl, value)
            return value
    except Exception:
        pass
    return None


def _cache_set(key: str, value, ttl: float) -> None:
    with _lock:
        _mem[key] = (time.time() + ttl, value)
    try:
        with (CACHE_DIR / f"{key}.pkl").open("wb") as fh:
            pickle.dump(value, fh)
    except Exception:
        pass


def _clean_frame(df: pd.DataFrame) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    df = df.dropna(how="all")
    if df.empty:
        return None
    rename = {}
    for col in df.columns:
        low = str(col).lower()
        mapping = {"open": "o", "high": "h", "low": "l", "close": "c",
                   "adj close": "adj", "volume": "v"}
        if low in mapping:
            rename[col] = mapping[low]
    df = df.rename(columns=rename)
    need = [c for c in ("o", "h", "l", "c") if c in df.columns]
    if len(need) < 4:
        return None
    df = df.dropna(subset=["o", "h", "l", "c"])
    if df.empty:
        return None
    if "v" not in df.columns:
        df["v"] = 0
    df["v"] = pd.to_numeric(df["v"], errors="coerce").fillna(0)
    df.index = pd.to_datetime(df.index, utc=True, errors="coerce").tz_convert("Asia/Kolkata")
    df = df[~df.index.isna()]
    return df[["o", "h", "l", "c", "v"]].astype(float)


def _download(symbols: list[str], period: str, interval: str) -> dict[str, pd.DataFrame]:
    tickers = [_suffix(s) for s in symbols]
    try:
        raw = yf.download(
            tickers=tickers, period=period, interval=interval, group_by="ticker",
            auto_adjust=True, threads=True, progress=False, prepost=False,
        )
    except Exception:
        return {}
    if raw is None or raw.empty:
        return {}

    out: dict[str, pd.DataFrame] = {}
    if isinstance(raw.columns, pd.MultiIndex):
        level0 = set(raw.columns.get_level_values(0))
        for ticker in tickers:
            if ticker not in level0:
                continue
            frame = _clean_frame(raw[ticker])
            if frame is not None:
                out[ticker[:-3]] = frame
    elif len(tickers) == 1:
        frame = _clean_frame(raw)
        if frame is not None:
            out[tickers[0][:-3]] = frame
    return out


def daily(symbols: list[str], period: str = "1y") -> dict[str, pd.DataFrame]:
    key = _key("yf_d", symbols, period)
    cached = _cache_get(key, _DAILY_TTL)
    if cached is not None:
        return cached  # type: ignore[return-value]
    data = _download(symbols, period, "1d")
    if data:
        _cache_set(key, data, _DAILY_TTL)
    return data


def intraday(symbols: list[str], interval: str = "5m", period: str = "5d") -> dict[str, pd.DataFrame]:
    key = _key("yf_i", symbols, interval, period)
    cached = _cache_get(key, _INTRADAY_TTL)
    if cached is not None:
        return cached  # type: ignore[return-value]
    data = _download(symbols, period, interval)
    if data:
        _cache_set(key, data, _INTRADAY_TTL)
    return data


def quote_batch(symbols: list[str]) -> list[dict]:
    """Delayed snapshot assembled from the last two 5m sessions.

    Yahoo's daily download usually contains the *current* partial bar, so using
    its last close as "previous close" would always report a 0% change. Instead
    the previous close comes from the prior intraday session, falling back to
    the daily bars only when the intraday window has a single session.
    """
    daily_data = daily(symbols, "5d")
    intra = intraday(symbols, "5m", "5d")
    now = dt.datetime.now().isoformat(timespec="seconds")
    out = []
    for sym in symbols:
        d = daily_data.get(sym)
        i = intra.get(sym)
        if (d is None or d.empty) and (i is None or i.empty):
            continue

        price = prev = o = h = l = v = None
        if i is not None and not i.empty:
            index = i.index
            current_day = index[-1].date()
            same_day = i[index.date == current_day]
            if same_day.empty:
                same_day = i.tail(1)
            price = float(same_day["c"].iloc[-1])
            o = float(same_day["o"].iloc[0])
            h = float(same_day["h"].max())
            l = float(same_day["l"].min())
            v = int(same_day["v"].sum())

            earlier = i[index.date < current_day]
            if not earlier.empty:
                prev = float(earlier["c"].iloc[-1])
            elif d is not None and not d.empty:
                daily_days = d.index.date
                if len(daily_days) and daily_days[-1] < current_day:
                    prev = float(d["c"].iloc[-1])
                elif len(d) >= 2:
                    prev = float(d["c"].iloc[-2])
        elif d is not None and not d.empty:
            price = float(d["c"].iloc[-1])
            o, h, l = float(d["o"].iloc[-1]), float(d["h"].iloc[-1]), float(d["l"].iloc[-1])
            v = int(d["v"].iloc[-1])
            if len(d) >= 2:
                prev = float(d["c"].iloc[-2])

        if price is None:
            continue
        change = (price - prev) if prev else 0.0
        out.append({
            "symbol": sym, "price": round(price, 2),
            "prev_close": round(prev, 2) if prev else None,
            "change": round(change, 2),
            "change_pct": round(change / prev * 100, 2) if prev else 0.0,
            "open": round(o, 2) if o else None,
            "high": round(h, 2) if h else None,
            "low": round(l, 2) if l else None,
            "volume": v, "delayed": True, "source": "yahoo", "as_of": now,
        })
    return out


def fundamentals(symbol: str) -> dict | None:
    """Best-effort NSE fundamentals. Returns None when unavailable."""
    sym = _suffix(symbol)
    key = _key("yf_f", [sym], "v1")
    cached = _cache_get(key, _FUND_TTL)
    if cached is not None:
        return cached or None
    try:
        info = yf.Ticker(sym).info or {}
    except Exception:
        _cache_set(key, {}, _FUND_TTL)
        return None

    def pick(*names):
        for name in names:
            value = info.get(name)
            if value is not None:
                return value
        return None

    data = {
        "pe": pick("trailingPE", "forwardPE"),
        "pb": pick("priceToBook"),
        "roe": pick("returnOnEquity"),
        "debt_to_equity": pick("debtToEquity"),
        "profit_margin": pick("profitMargins"),
        "revenue_growth": pick("revenueGrowth"),
        "earnings_growth": pick("earningsGrowth"),
        "market_cap": pick("marketCap"),
        "sector": pick("sector"),
        "industry": pick("industry"),
        "name": pick("longName", "shortName"),
    }
    data = {k: v for k, v in data.items() if v is not None}
    _cache_set(key, data, _FUND_TTL)
    return data or None
