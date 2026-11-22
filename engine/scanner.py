"""Full-universe scan pipeline (V4 multi-timeframe, closed-candle aware)."""
from __future__ import annotations

import datetime as dt
import re
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from engine import indicators, marketdata, patterns, quality, scoring, universe, yahoo
from engine.settings import IST, cfg, now_ist


def _interval_minutes(value) -> int:
    text = str(value or "5m").strip().lower()
    match = re.match(r"(\d+)\s*(m|min|h|hour|d|day)", text)
    if not match:
        return 5
    amount = int(match.group(1))
    unit = match.group(2)
    if unit.startswith("h"):
        return amount * 60
    if unit.startswith("d"):
        return amount * 1440
    return amount


def _drop_forming_candle(frame: pd.DataFrame | None, minutes: int) -> pd.DataFrame | None:
    """V4 uses closed candles only; drop the current partial intraday bar."""
    if frame is None or len(frame) < 6 or not isinstance(frame.index, pd.DatetimeIndex):
        return frame
    last = frame.index[-1]
    try:
        last = last.tz_localize(IST) if last.tzinfo is None else last.tz_convert(IST)
    except (TypeError, AttributeError):
        return frame
    now = now_ist()
    if minutes >= 60:
        hours = max(1, minutes // 60)
        boundary = now.replace(hour=(now.hour // hours) * hours, minute=0,
                               second=0, microsecond=0)
    else:
        boundary = now.replace(minute=(now.minute // minutes) * minutes,
                               second=0, microsecond=0)
    if last >= boundary:
        return frame.iloc[:-1]
    return frame


def _prepare(frame: pd.DataFrame | None, intraday: bool,
             minutes: int = 5) -> pd.DataFrame | None:
    if frame is None or len(frame) < 5:
        return frame
    if intraday and cfg("engine", "use_closed_candles_only", default=True):
        frame = _drop_forming_candle(frame, minutes)
    if frame is None or len(frame) < 5:
        return frame
    return indicators.enrich(frame, intraday=intraday)


def _quote_age_sec(quote: dict) -> float | None:
    raw = quote.get("as_of")
    if not raw:
        return None
    try:
        stamp = dt.datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=IST)
    return max(0.0, (now_ist() - stamp).total_seconds())


def scan(symbols: list[str] | None = None, with_fundamentals: bool | None = None) -> dict:
    started = time.time()
    symbols = symbols or universe.load_symbols()
    source = marketdata.source()
    interval = str(cfg("indicators", "execution_timeframe", default="5m"))
    minutes = _interval_minutes(interval)
    trend_interval = str(cfg("indicators", "trend_timeframe", default="15m"))
    snap = marketdata.snapshot(symbols, intraday_interval=interval)
    lookback = int(cfg("entry", "breakout_lookback", default=20))
    max_age = float(cfg("data", "validation", "max_quote_age_seconds", default=10))
    reject_stale = bool(cfg("data", "validation", "reject_stale_data", default=True))
    closed_only = bool(cfg("engine", "use_closed_candles_only", default=True))

    results: list[dict] = []
    errors: list[str] = []
    pattern_map: dict[str, dict] = {}
    mtf_map: dict[str, pd.DataFrame | None] = {}
    for sym, data in snap.items():
        try:
            daily = _prepare(data.get("daily"), intraday=False)
            intra = _prepare(data.get("intraday"), intraday=True, minutes=minutes)
            quote = data["quote"]

            ok, reason = universe.passes_filters(sym, daily, quote)
            if not ok:
                continue

            trend_frame = indicators.resample(intra, trend_interval) if intra is not None else None
            mtf_map[sym] = trend_frame

            pattern_info = patterns.detect_all(daily, intra, lookback=lookback)
            pattern_map[sym] = pattern_info
            quality_info = quality.quality_score(daily)
            scored = scoring.score_symbol(sym, quote, daily, intra, pattern_info,
                                          quality_info, trend_frame=trend_frame)

            atr_pct = float(scored["indicators"].get("atr_pct") or 0)
            scored["atr"] = round(float(quote["price"]) * atr_pct / 100, 2) if atr_pct else None
            scored["fundamentals"] = None
            scored["data_source"] = str(quote.get("source") or source)
            scored["delayed"] = bool(quote.get("delayed", source != "groww"))
            age = _quote_age_sec(quote)
            scored["quote_age_sec"] = round(age, 1) if age is not None else None
            scored["data_stale"] = bool(reject_stale and age is not None and age > max_age)
            scored["closed_candle"] = bool(closed_only)

            price_now = float(quote.get("price") or 0)
            inds = scored["indicators"]
            ema5_20 = indicators.last(intra, "ema20", 0.0) if intra is not None else 0.0
            ema5_50 = indicators.last(intra, "ema50", 0.0) if intra is not None else 0.0
            ema15_20 = indicators.last(trend_frame, "ema20", 0.0) if trend_frame is not None else 0.0
            ema15_50 = indicators.last(trend_frame, "ema50", 0.0) if trend_frame is not None else 0.0
            ema15_200 = indicators.last(trend_frame, "ema200", 0.0) if trend_frame is not None else 0.0
            inds["ema20_5m"] = round(ema5_20, 2)
            inds["ema50_5m"] = round(ema5_50, 2)
            inds["ema20_15m"] = round(ema15_20, 2)
            inds["ema50_15m"] = round(ema15_50, 2)
            inds["ema200_15m"] = round(ema15_200, 2)
            scored["trend_confirmation"] = {
                "5m": bool(price_now and ema5_20 and ema5_50 and price_now > ema5_20 > ema5_50),
                "15m": bool(
                    price_now and ema15_20 and ema15_50 and price_now > ema15_20 > ema15_50
                    and (not ema15_200 or ema15_50 > ema15_200)
                ),
            }
            results.append(scored)
        except Exception as exc:  # keep scanning the rest of the universe
            errors.append(f"{sym}: {exc}")

    results.sort(key=lambda r: r["score"], reverse=True)

    enrich = cfg("fundamentals", "enabled", default=True) if with_fundamentals is None else with_fundamentals
    if enrich and results:
        top = [r for r in results if r["score"] >= float(
            cfg("scoring", "thresholds", "watchlist", default=55))][:15]
        if top:
            def _enrich(item: dict) -> None:
                try:
                    fund = yahoo.fundamentals(item["symbol"])
                    if fund:
                        item["fundamentals"] = fund
                        data = snap.get(item["symbol"], {})
                        daily = _prepare(data.get("daily"), intraday=False)
                        intra = _prepare(data.get("intraday"), intraday=True, minutes=minutes)
                        if daily is not None:
                            q = quality.quality_score(daily, fund)
                            item["quality"] = q
                            refreshed = scoring.score_symbol(
                                item["symbol"], data["quote"], daily, intra,
                                pattern_map.get(item["symbol"]) or {
                                    "patterns": item["patterns"], "pattern_score": 50.0,
                                    "breakout": item.get("breakout", False),
                                    "support": item.get("support"),
                                    "resistance": item.get("resistance"),
                                },
                                q, trend_frame=mtf_map.get(item["symbol"]),
                            )
                            item["buckets"] = refreshed["buckets"]
                            item["score"] = refreshed["score"]
                            item["status"] = refreshed["status"]
                            item["confirmations"] = refreshed["confirmations"]
                except Exception:
                    pass

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(_enrich, top))
            results.sort(key=lambda r: r["score"], reverse=True)

    qualified = sum(1 for r in results if r["status"] == "QUALIFIED")
    watchlist = sum(1 for r in results if r["status"] == "WATCHLIST")

    return {
        "ts": now_ist().isoformat(timespec="seconds"),
        "source": source,
        "interval": interval,
        "scanned": len(snap),
        "eligible": len(results),
        "qualified": qualified,
        "watchlist": watchlist,
        "duration_ms": int((time.time() - started) * 1000),
        "results": results,
        "errors": errors[:10],
    }
