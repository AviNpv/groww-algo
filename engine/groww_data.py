"""Groww market-data client — READ ONLY.

Implements only:
    GET /v1/user/detail, /v1/live-data/quote, /v1/live-data/ltp,
    /v1/historical/candles, and the public instrument master CSV.

No order / modify / cancel / GTT endpoint exists in this module, so it
cannot place a real order even if credentials are present.
"""
from __future__ import annotations

import csv
import datetime as dt
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from engine import groww_auth
from engine.settings import CACHE_DIR

BASE = "https://api.groww.in"
INSTRUMENT_CSV = "https://growwapi-assets.groww.in/instruments/instrument.csv"
CSV_CACHE = CACHE_DIR / "groww_instruments.csv"

_MIN_INTERVAL = 0.11          # 10 req/sec bucket
_rl_lock = threading.Lock()
_rl_last = 0.0

_symbol_lock = threading.Lock()
_symbol_cache: dict[str, str] | None = None

_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


class GrowwUnavailable(RuntimeError):
    pass


class GrowwForbidden(GrowwUnavailable):
    """Token is valid but the account lacks the market-data entitlement."""


_DATA_PATHS = ("/v1/live-data", "/v1/historical")


def _error_detail(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:160]
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])
    return resp.text[:160]


def _headers() -> dict[str, str]:
    try:
        tok = groww_auth.get_token()
    except groww_auth.GrowwAuthError as exc:
        raise GrowwUnavailable(f"authentication failed ({exc.stage}): {exc.message}") from exc
    return {"Authorization": f"Bearer {tok}", "Accept": "application/json", "X-API-VERSION": "1.0"}


def _throttle() -> None:
    global _rl_last
    with _rl_lock:
        wait = _MIN_INTERVAL - (time.time() - _rl_last)
        if wait > 0:
            time.sleep(wait)
        _rl_last = time.time()


def _get(path: str, params: dict) -> dict:
    _throttle()
    try:
        resp = requests.get(BASE + path, params=params, headers=_headers(), timeout=15)
    except GrowwUnavailable:
        raise
    except requests.RequestException as exc:
        raise GrowwUnavailable(f"network error: {exc}") from exc

    if resp.status_code == 401:
        groww_auth.invalidate()
        raise GrowwUnavailable("Groww rejected the access token (401); cache cleared")
    if resp.status_code == 403:
        detail = _error_detail(resp)
        if any(path.startswith(p) for p in _DATA_PATHS):
            raise GrowwForbidden(
                f"Groww 403 on {path}: {detail}. The token works, but live quotes and "
                "candles need a Groww market-data subscription on this API key.")
        raise GrowwUnavailable(f"Groww 403 on {path}: {detail}")
    if resp.status_code == 429:
        raise GrowwUnavailable("Groww rate limit hit (429)")
    if resp.status_code != 200:
        raise GrowwUnavailable(f"Groww HTTP {resp.status_code} on {path}: {_error_detail(resp)}")

    data = resp.json()
    if str(data.get("status", "")).upper() != "SUCCESS":
        err = data.get("error") or {}
        raise GrowwUnavailable(f"Groww error {err.get('code')}: {err.get('message')}")
    return data.get("payload") or {}


_USER_TTL = 300.0
_user_cache: dict[str, tuple[float, dict]] = {}


def user_detail(ttl: float = _USER_TTL) -> dict:
    """Account profile from Groww (UCC, enabled exchanges, active segments)."""
    with _cache_lock:
        hit = _user_cache.get("detail")
    if hit and hit[0] > time.time():
        return hit[1]
    payload = _get("/v1/user/detail", {})
    detail = {
        "vendor_user_id": payload.get("vendor_user_id"),
        "ucc": payload.get("ucc"),
        "nse_enabled": bool(payload.get("nse_enabled")),
        "bse_enabled": bool(payload.get("bse_enabled")),
        "ddpi_enabled": bool(payload.get("ddpi_enabled")),
        "active_segments": list(payload.get("active_segments") or []),
    }
    with _cache_lock:
        _user_cache["detail"] = (time.time() + ttl, detail)
    return detail


_HOLDINGS_TTL = 60.0
_POSITIONS_TTL = 20.0
_ORDERS_TTL = 20.0


def _fnum(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def holdings(ttl: float = _HOLDINGS_TTL) -> list[dict]:
    """Long-term demat holdings from Groww (/v1/holdings/user)."""
    with _cache_lock:
        hit = _cache.get("holdings")
    if hit and hit[0] > time.time():
        return [dict(row) for row in hit[1]]  # type: ignore[index]
    payload = _get("/v1/holdings/user", {})
    rows = []
    for item in payload.get("holdings") or []:
        symbol = (item.get("trading_symbol") or "").strip().upper()
        if not symbol:
            continue
        rows.append({
            "symbol": symbol,
            "isin": item.get("isin"),
            "quantity": _fnum(item.get("quantity")),
            "average_price": _fnum(item.get("average_price")),
            "free_quantity": _fnum(item.get("demat_free_quantity")),
            "t1_quantity": _fnum(item.get("t1_quantity")),
            "pledged_quantity": _fnum(item.get("pledge_quantity")),
            "locked_quantity": _fnum(item.get("demat_locked_quantity"))
                              + _fnum(item.get("groww_locked_quantity")),
            "exchanges": list(item.get("tradable_exchanges") or []),
        })
    rows.sort(key=lambda row: row["symbol"])
    with _cache_lock:
        _cache["holdings"] = (time.time() + ttl, rows)
    return [dict(row) for row in rows]


def positions(ttl: float = _POSITIONS_TTL) -> list[dict]:
    """Open positions from Groww (/v1/positions/user)."""
    with _cache_lock:
        hit = _cache.get("positions")
    if hit and hit[0] > time.time():
        return [dict(row) for row in hit[1]]  # type: ignore[index]
    payload = _get("/v1/positions/user", {})
    rows = []
    for item in payload.get("positions") or []:
        symbol = (item.get("trading_symbol") or "").strip().upper()
        if not symbol:
            continue
        rows.append({
            "symbol": symbol,
            "exchange": item.get("exchange"),
            "product": item.get("product"),
            "quantity": _fnum(item.get("quantity")),
            "net_price": _fnum(item.get("net_price")),
            "realised_pnl": _fnum(item.get("realised_pnl")),
            "isin": item.get("symbol_isin"),
        })
    with _cache_lock:
        _cache["positions"] = (time.time() + ttl, rows)
    return [dict(row) for row in rows]


def orders(ttl: float = _ORDERS_TTL) -> list[dict]:
    """Today's order list from Groww (/v1/order/list)."""
    with _cache_lock:
        hit = _cache.get("orders")
    if hit and hit[0] > time.time():
        return [dict(row) for row in hit[1]]  # type: ignore[index]
    payload = _get("/v1/order/list", {})
    rows = []
    for item in payload.get("order_list") or []:
        rows.append({
            "order_id": item.get("groww_order_id") or item.get("order_id"),
            "symbol": item.get("trading_symbol"),
            "status": item.get("order_status"),
            "side": item.get("transaction_type"),
            "quantity": _fnum(item.get("quantity")),
            "filled": _fnum(item.get("filled_quantity")),
            "price": _fnum(item.get("price")),
            "average_fill_price": _fnum(item.get("average_fill_price")),
            "order_type": item.get("order_type"),
            "product": item.get("product"),
            "exchange": item.get("exchange"),
            "created_at": item.get("created_at"),
        })
    with _cache_lock:
        _cache["orders"] = (time.time() + ttl, rows)
    return [dict(row) for row in rows]


def health() -> dict:
    auth = groww_auth.status()
    out = {"ok": False, "stage": None, "state": auth.get("state"),
           "message": auth.get("message"), "auth": auth,
           "portfolio_ok": False, "live_data": False}
    if auth["state"] in ("no_api_key", "no_secret") and not auth["token_cached"]:
        out["stage"] = "config"
        return out
    try:
        groww_auth.get_token()
    except groww_auth.GrowwAuthError as exc:
        out.update(stage="auth", state=exc.stage, message=exc.message)
        return out
    try:
        payload = _get("/v1/user/detail", {})
    except GrowwUnavailable as exc:
        out.update(stage="account", state="account_error", message=str(exc))
        return out

    # Portfolio endpoints use the non-trading scope, which is separate from the
    # market-data entitlement. Tracking them separately stops the UI from
    # claiming "disconnected" when holdings actually work fine.
    try:
        holdings(ttl=0)
        positions(ttl=0)
        out["portfolio_ok"] = True
    except GrowwUnavailable:
        out["portfolio_ok"] = False

    try:
        _get("/v1/live-data/quote", {"exchange": "NSE", "segment": "CASH", "trading_symbol": "RELIANCE"})
    except GrowwForbidden as exc:
        out.update(stage="data", state="data_forbidden", message=str(exc))
        return out
    except GrowwUnavailable as exc:
        out.update(stage="data", state="data_error", message=str(exc))
        return out
    out.update(ok=True, stage="live", state="live", live_data=True,
               message="Groww real-time market data authorised",
               user={"ucc": payload.get("ucc"),
                     "active_segments": payload.get("active_segments")})
    return out


# --------------------------------------------------------------------------
# symbol master
# --------------------------------------------------------------------------
def _load_symbols() -> dict[str, str]:
    global _symbol_cache
    with _symbol_lock:
        if _symbol_cache is not None:
            return _symbol_cache
        try:
            if not CSV_CACHE.exists() or time.time() - CSV_CACHE.stat().st_mtime > 7 * 86400:
                resp = requests.get(INSTRUMENT_CSV, timeout=60)
                resp.raise_for_status()
                CSV_CACHE.write_bytes(resp.content)
            mapping: dict[str, str] = {}
            with CSV_CACHE.open("r", encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    if row.get("segment") != "CASH" or row.get("exchange") != "NSE":
                        continue
                    ts = (row.get("trading_symbol") or "").strip()
                    gs = (row.get("groww_symbol") or "").strip()
                    if ts and gs and (row.get("series") or "").strip() == "EQ" and ts not in mapping:
                        mapping[ts] = gs
            if not mapping:
                raise GrowwUnavailable("instrument master had no NSE EQ symbols")
            _symbol_cache = mapping
            return mapping
        except GrowwUnavailable:
            raise
        except Exception as exc:
            raise GrowwUnavailable(f"instrument master unavailable: {exc}") from exc


def bare(symbol: str) -> str:
    ts = symbol.strip().upper()
    for suffix in (".NS", ".BO"):
        if ts.endswith(suffix):
            ts = ts[: -len(suffix)]
    return ts


def groww_symbol(symbol: str) -> str:
    ts = bare(symbol)
    try:
        mapping = _load_symbols()
        if ts in mapping:
            return mapping[ts]
    except GrowwUnavailable:
        pass
    return f"NSE-{ts}"


def _num(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# quotes
# --------------------------------------------------------------------------
def quote(symbol: str) -> dict:
    sym = groww_symbol(symbol)
    ts = sym.split("-", 1)[1] if sym.startswith("NSE-") else sym
    payload = _get("/v1/live-data/quote",
                   {"exchange": "NSE", "segment": "CASH", "trading_symbol": ts})
    last = _num(payload.get("last_price"))
    if last is None:
        raise GrowwUnavailable(f"no last_price for {ts}")

    change = _num(payload.get("day_change")) or 0.0
    pct = _num(payload.get("day_change_perc")) or 0.0
    prev = last - change if change else None
    ohlc = payload.get("ohlc") if isinstance(payload.get("ohlc"), dict) else {}
    ltt = payload.get("last_trade_time")
    as_of = (dt.datetime.fromtimestamp(int(ltt) / 1000).isoformat(timespec="seconds")
             if ltt else dt.datetime.now().isoformat(timespec="seconds"))
    return {
        "symbol": bare(symbol),
        "trading_symbol": ts,
        "price": round(last, 2),
        "prev_close": round(prev, 2) if prev else None,
        "change": round(change, 2),
        "change_pct": round(pct, 2),
        "open": _num(ohlc.get("open")),
        "high": _num(ohlc.get("high")),
        "low": _num(ohlc.get("low")),
        "volume": int(payload["volume"]) if payload.get("volume") else None,
        "week_52_high": _num(payload.get("week_52_high")),
        "week_52_low": _num(payload.get("week_52_low")),
        "delayed": False,
        "source": "groww",
        "as_of": as_of,
    }


def quotes(symbols: list[str], ttl: float = 8.0) -> list[dict]:
    if not symbols:
        return []
    key = "quotes:" + ",".join(sorted(symbols))
    with _cache_lock:
        hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]  # type: ignore[return-value]

    def _one(sym: str):
        try:
            return quote(sym)
        except GrowwUnavailable:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_one, symbols))
    out = [r for r in results if r]
    if not out:
        raise GrowwUnavailable("no quotes could be fetched from Groww")
    with _cache_lock:
        _cache[key] = (time.time() + ttl, out)
    return out


_INTERVAL_MAP = {
    "1m": ("1minute", 5), "2m": ("2minute", 10), "3m": ("3minute", 15),
    "5m": ("5minute", 20), "10m": ("10minute", 40), "15m": ("15minute", 60),
    "30m": ("30minute", 80), "60m": ("1hour", 150), "1h": ("1hour", 150),
    "4h": ("4hour", 170), "1d": ("1day", 170), "1wk": ("1week", 300),
}


def candles(symbol: str, interval: str = "5m", days: int | None = None) -> dict:
    interval = interval if interval in _INTERVAL_MAP else "5m"
    candle_interval, default_days = _INTERVAL_MAP[interval]
    days = int(days or default_days)
    sym = groww_symbol(symbol)
    end = dt.datetime.now()
    start = end - dt.timedelta(days=days)
    payload = _get("/v1/historical/candles", {
        "exchange": "NSE", "segment": "CASH", "groww_symbol": sym,
        "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
        "candle_interval": candle_interval,
    })
    rows = payload.get("candles") or []
    out = []
    for row in rows:
        if len(row) < 6:
            continue
        ts, o, h, l, c, v = row[:6]
        out.append({"t": str(ts), "o": float(o), "h": float(h),
                    "l": float(l), "c": float(c), "v": int(v or 0)})
    return {"symbol": bare(symbol), "interval": interval, "delayed": False,
            "source": "groww", "candles": out}


def ltp(symbols: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for i in range(0, len(symbols), 50):
        chunk = symbols[i: i + 50]
        param = ",".join("NSE_" + bare(s) for s in chunk)
        payload = _get("/v1/live-data/ltp", {"segment": "CASH", "exchange_symbols": param})
        for key, value in payload.items():
            out[key] = float(value)
    return out
