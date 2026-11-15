"""Full-universe scan pipeline."""
from __future__ import annotations

import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from engine import indicators, marketdata, patterns, quality, scoring, universe, yahoo
from engine.settings import cfg


def _prepare(frame: pd.DataFrame | None, intraday: bool) -> pd.DataFrame | None:
    if frame is None or len(frame) < 5:
        return frame
    return indicators.enrich(frame, intraday=intraday)


def scan(symbols: list[str] | None = None, with_fundamentals: bool | None = None) -> dict:
    started = time.time()
    symbols = symbols or universe.load_symbols()
    source = marketdata.source()
    snap = marketdata.snapshot(symbols)
    lookback = int(cfg("entry", "breakout_lookback", default=20))

    results: list[dict] = []
    errors: list[str] = []
    pattern_map: dict[str, dict] = {}
    for sym, data in snap.items():
        try:
            daily = _prepare(data.get("daily"), intraday=False)
            intra = _prepare(data.get("intraday"), intraday=True)
            quote = data["quote"]

            ok, reason = universe.passes_filters(sym, daily, quote)
            if not ok:
                continue

            pattern_info = patterns.detect_all(daily, intra, lookback=lookback)
            pattern_map[sym] = pattern_info
            quality_info = quality.quality_score(daily)
            scored = scoring.score_symbol(sym, quote, daily, intra, pattern_info, quality_info)

            atr_pct = float(scored["indicators"].get("atr_pct") or 0)
            scored["atr"] = round(float(quote["price"]) * atr_pct / 100, 2) if atr_pct else None
            scored["fundamentals"] = None
            results.append(scored)
        except Exception as exc:  # keep scanning the rest of the universe
            errors.append(f"{sym}: {exc}")

    results.sort(key=lambda r: r["score"], reverse=True)

    enrich = cfg("fundamentals", "enabled", default=True) if with_fundamentals is None else with_fundamentals
    if enrich and results:
        top = [r for r in results if r["score"] >= float(cfg("scoring", "watchlist_score", default=55))][:15]
        if top:
            def _enrich(item: dict) -> None:
                try:
                    fund = yahoo.fundamentals(item["symbol"])
                    if fund:
                        item["fundamentals"] = fund
                        data = snap.get(item["symbol"], {})
                        daily = _prepare(data.get("daily"), intraday=False)
                        intra = _prepare(data.get("intraday"), intraday=True)
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
                                q,
                            )
                            item["buckets"] = refreshed["buckets"]
                            item["score"] = refreshed["score"]
                            item["status"] = refreshed["status"]
                except Exception:
                    pass

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(_enrich, top))
            results.sort(key=lambda r: r["score"], reverse=True)

    qualified = sum(1 for r in results if r["score"] >= float(cfg("scoring", "qualified_score", default=65)))
    watchlist = sum(1 for r in results if r["status"] == "WATCHLIST")

    return {
        "ts": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(timespec="seconds"),
        "source": source,
        "scanned": len(snap),
        "eligible": len(results),
        "qualified": qualified,
        "watchlist": watchlist,
        "duration_ms": int((time.time() - started) * 1000),
        "results": results,
        "errors": errors[:10],
    }
