"""Paths, config loading and IST market-clock helpers."""
from __future__ import annotations

import datetime as dt
import os
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "algo.sqlite"
CONFIG_PATH = CONFIG_DIR / "strategy.yaml"

DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

IST = ZoneInfo("Asia/Kolkata")

_lock = threading.RLock()
_cache: dict = {"mtime": 0.0, "config": {}}


def load_config(force: bool = False) -> dict:
    """Read strategy.yaml, reloading automatically when the file changes."""
    with _lock:
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except OSError:
            mtime = 0.0
        if not force and _cache["config"] and _cache["mtime"] == mtime:
            return _cache["config"]
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        _cache["config"] = data
        _cache["mtime"] = mtime
        return data


def cfg(*keys, default=None):
    """Nested config lookup: cfg('entry', 'min_score', default=65)."""
    node = load_config()
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def param(params: dict | None, section: str, key: str, default):
    """Read a rule from an explicit parameter set, else from strategy.yaml.

    The live engine passes params=None (so it always follows the file). The
    backtester passes a merged dict so it can try different strategies without
    writing to config/strategy.yaml.
    """
    if params:
        node = params.get(section)
        if isinstance(node, dict) and key in node:
            return node[key]
    return cfg(section, key, default=default)


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def _parse_hhmm(value: str, fallback: dt.time) -> dt.time:
    try:
        hh, mm = str(value).split(":")[:2]
        return dt.time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return fallback


def market_session() -> tuple[dt.datetime, dt.datetime]:
    """Today's open/close as IST datetimes."""
    now = now_ist()
    open_t = _parse_hhmm(cfg("engine", "entry_window", default=["09:15", "15:30"])[0], dt.time(9, 15))
    close_t = _parse_hhmm(cfg("engine", "square_off", default="15:20"), dt.time(15, 20))
    return (
        now.replace(hour=open_t.hour, minute=open_t.minute, second=0, microsecond=0),
        now.replace(hour=close_t.hour, minute=close_t.minute, second=0, microsecond=0),
    )


def is_trading_day(day: dt.date | None = None) -> bool:
    day = day or now_ist().date()
    if day.weekday() >= 5:  # Saturday / Sunday
        return False
    holidays = cfg("engine", "holidays", default=[]) or []
    return day.isoformat() not in {str(h) for h in holidays}


def is_market_open(now: dt.datetime | None = None) -> bool:
    """NSE cash session: 09:15-15:30 IST on trading days."""
    now = now or now_ist()
    if not is_trading_day(now.date()):
        return False
    start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return start <= now <= end


def in_entry_window(now: dt.datetime | None = None) -> bool:
    now = now or now_ist()
    if not is_trading_day(now.date()):
        return False
    start_s, end_s = cfg("engine", "entry_window", default=["09:20", "14:45"])[:2]
    start = now.replace(hour=int(start_s[:2]), minute=int(start_s[3:5]), second=0, microsecond=0)
    end = now.replace(hour=int(end_s[:2]), minute=int(end_s[3:5]), second=0, microsecond=0)
    return start <= now <= end


def env(key: str, default: str = "") -> str:
    import os

    return (os.environ.get(key) or default).strip()
