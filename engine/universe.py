"""Trading universe loading and filtering."""
from __future__ import annotations

from pathlib import Path

from engine.settings import BASE_DIR, cfg, param


def load_symbols() -> list[str]:
    rel = cfg("universe", "file", default="config/nifty500.txt")
    path = Path(rel)
    if not path.is_absolute():
        path = BASE_DIR / path
    symbols: list[str] = []
    seen: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip().upper()
            if not line:
                continue
            for suffix in (".NS", ".BO"):
                if line.endswith(suffix):
                    line = line[: -len(suffix)]
            if line and line not in seen:
                seen.add(line)
                symbols.append(line)

    exclude = {str(x).upper().replace(".NS", "") for x in (cfg("universe", "exclude", default=[]) or [])}
    symbols = [s for s in symbols if s not in exclude]
    cap = int(cfg("universe", "max_symbols", default=140))
    return symbols[:cap]


def passes_filters(symbol: str, daily, quote: dict | None = None,
                   params: dict | None = None) -> tuple[bool, str]:
    min_price = float(param(params, "universe", "min_price", 30))
    min_value = float(param(params, "universe", "min_avg_daily_value", 2e7))
    price = float((quote or {}).get("price") or 0)
    if price and price < min_price:
        return False, f"price {price:.2f} below {min_price:.0f}"
    if daily is None or len(daily) < 25:
        return False, "insufficient history"
    try:
        avg_value = float((daily["c"] * daily["v"]).tail(20).mean())
    except Exception:
        return False, "no turnover data"
    if avg_value < min_value:
        return False, f"turnover {avg_value / 1e7:.1f}cr below {min_value / 1e7:.0f}cr"
    return True, ""
