"""Paths, config loading, IST market-clock helpers, and V4 config compatibility.

The file on disk is the user-authored strategy document. V4 renamed most
sections, so the runtime layer adds V2-compatible aliases on a deep copy. The
raw document returned to the settings editor is never modified.
"""
from __future__ import annotations

import copy
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
_cache: dict = {"mtime": 0.0, "raw": {}, "config": {}}


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _get(node, *path, default=None):
    cur = node
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _first(*values):
    """First non-None value; config nulls are treated as absent."""
    for value in values:
        if value is not None:
            return value
    return None


def _alias(node: dict, key: str, value) -> None:
    """Add a compatibility alias without overwriting an explicit key."""
    if value is not None and key not in node:
        node[key] = value


def _normalize(data: dict) -> dict:
    """Return a runtime view of the config with legacy lookup keys filled in."""
    cfg = copy.deepcopy(data)

    app = _dict(cfg.get("app"))
    _alias(cfg, "mode", _first(app.get("mode"), cfg.get("mode"), "paper"))

    engine = _dict(cfg.get("engine"))
    data_node = _dict(cfg.get("data"))
    groww = _dict(data_node.get("groww"))
    yahoo = _dict(data_node.get("yahoo_finance"))
    fallback = _dict(data_node.get("fallback"))
    validation = _dict(data_node.get("validation"))

    source = _first(data_node.get("source_mode"), engine.get("data_source"), "auto")
    source = "yahoo" if str(source).lower() == "yahoo_finance" else str(source).lower()

    entry_window = _first(
        engine.get("entry_window"),
        [_first(engine.get("entry_start_time"), "09:25"),
         _first(engine.get("entry_end_time"), "14:45")],
    )
    _alias(engine, "autotrade", _first(engine.get("auto_trade"), engine.get("autotrade"), True))
    _alias(engine, "scan_interval_sec", _first(engine.get("scan_interval_seconds"), 300))
    _alias(engine, "quote_refresh_sec", _first(engine.get("quote_refresh_seconds"), 20))
    _alias(engine, "entry_window", entry_window)
    _alias(engine, "square_off", _first(engine.get("square_off_time"), "15:20"))
    _alias(engine, "data_source", source)
    _alias(engine, "holidays", [])

    groww_confirmation = _first(
        fallback.get("groww_confirmation_required"),
        True if yahoo.get("use_for_entry_confirmation") is False else None,
        True,
    )
    fallback_map = {
        "enabled": _first(fallback.get("enabled"), True),
        "primary": _first(data_node.get("primary_source"), "groww"),
        "secondary": _first(data_node.get("secondary_source"), "yahoo_finance"),
        "fallback_on_error": _first(fallback.get("on_error"), True),
        "fallback_on_timeout": _first(fallback.get("on_timeout"), True),
        "fallback_on_empty_data": _first(fallback.get("on_empty_data"), True),
        "fallback_on_invalid_data": _first(fallback.get("on_invalid_data"), True),
        "max_quote_age_sec": _first(validation.get("max_quote_age_seconds"), 10),
        "reject_stale_data": _first(validation.get("reject_stale_data"), True),
        "validate_ohlcv": _first(validation.get("reject_missing_ohlcv"), True),
        "verify_timestamps": True,
        "allow_fallback_for_scanning": _first(
            fallback.get("yahoo_allowed_for_scanning"),
            yahoo.get("use_for_scanning"),
            True,
        ),
        "allow_fallback_for_trade_entries": not bool(groww_confirmation),
        "require_primary_confirmation_for_entries": bool(groww_confirmation),
        "auto_restore_primary": True,
    }
    fallback_map["secondary"] = (
        "yahoo" if str(fallback_map["secondary"]).lower() == "yahoo_finance"
        else fallback_map["secondary"]
    )
    runtime_fallback = _dict(engine.get("data_fallback"))
    for key, value in fallback_map.items():
        _alias(runtime_fallback, key, value)
    engine["data_fallback"] = runtime_fallback
    cfg["engine"] = engine

    account = _dict(cfg.get("account"))
    risk = _dict(cfg.get("risk"))
    _alias(account, "starting_capital", _first(account.get("initial_capital"), 10000))
    _alias(account, "risk_per_trade", _first(account.get("risk_per_trade"), risk.get("risk_per_trade"), 100))
    _alias(account, "max_daily_loss", _first(account.get("max_daily_loss"), risk.get("maximum_daily_loss"), 200))
    _alias(account, "max_positions", _first(account.get("max_positions"), risk.get("maximum_open_positions"), 2))
    _alias(account, "max_trades_per_day", _first(account.get("max_trades_per_day"), risk.get("maximum_trades_per_day"), 10))
    _alias(account, "max_trades_per_symbol_per_day", _first(
        account.get("max_trades_per_symbol_per_day"), risk.get("maximum_trades_per_symbol"), 1))
    _alias(account, "minimum_order_value", _first(account.get("minimum_order_value"), 100))
    cooldown = _dict(risk.get("cooldown"))
    consecutive = _dict(risk.get("consecutive_losses"))
    _alias(account, "cooldown_after_loss_minutes", _first(
        cooldown.get("after_loss_minutes"), 15))
    _alias(account, "max_consecutive_losses", _first(consecutive.get("maximum"), 3))
    sizing = _dict(account.get("position_sizing"))
    _alias(account, "position_sizing", _first(sizing.get("method"), "stop_distance_and_capital"))
    cfg["account"] = account

    costs = _dict(cfg.get("costs"))
    brokerage = _dict(costs.get("brokerage"))
    _alias(costs, "brokerage_per_order", _first(brokerage.get("per_order"), 20))
    _alias(costs, "brokerage_pct", _first(brokerage.get("percentage"), 0.03))
    _alias(costs, "exchange_txn_pct", _first(costs.get("exchange_transaction_charge_pct"), 0.00297))
    _alias(costs, "sebi_pct", _first(costs.get("sebi_charge_pct"), 0.0001))
    _alias(costs, "stamp_buy_pct", _first(costs.get("stamp_duty_buy_pct"), 0.003))
    cfg["costs"] = costs

    universe = _dict(cfg.get("universe"))
    filters = _dict(universe.get("filters"))
    scanner = _dict(universe.get("scanner"))
    _alias(universe, "file", _first(universe.get("symbols_file"), "config/nifty500.txt"))
    _alias(universe, "min_price", _first(filters.get("minimum_price"), 30))
    _alias(universe, "min_avg_daily_value", _first(filters.get("minimum_average_daily_value"), 2e7))
    for old, new in (
        ("exclude_sme", "exclude_sme"),
        ("exclude_etf", "exclude_etf"),
        ("exclude_suspended", "exclude_suspended"),
        ("exclude_trade_to_trade", "exclude_t2t"),
        ("exclude_illiquid", "exclude_illiquid"),
    ):
        _alias(universe, old, filters.get(new))
    scan = _dict(cfg.get("scan"))
    _alias(scan, "max_results", _first(scanner.get("max_candidates"), 200))
    _alias(scan, "rank_all_symbols", _first(scanner.get("rank_by_score"), True))
    _alias(scan, "sort_by", "score_descending")
    cfg["universe"] = universe
    cfg["scan"] = scan

    market = _dict(cfg.get("market_filter"))
    _alias(market, "use_market_trend", _first(market.get("use_market_direction"), True))
    _alias(market, "use_market_volatility_filter", _first(market.get("reject_if_market_data_missing"), True))
    _alias(market, "avoid_extreme_market_volatility", _first(market.get("reject_if_market_data_missing"), True))
    cfg["market_filter"] = market

    entry = _dict(cfg.get("entry"))
    entry_trend = _dict(entry.get("trend"))
    entry_mom = _dict(entry.get("momentum"))
    entry_vol = _dict(entry.get("volume"))
    entry_action = _dict(entry.get("price_action"))
    entry_volatility = _dict(entry.get("volatility"))
    entry_risk = _dict(entry.get("risk"))
    thresholds = _dict(_dict(cfg.get("scoring")).get("thresholds"))
    _alias(entry, "min_score", _first(entry.get("minimum_score"), thresholds.get("entry"), 65))
    _alias(entry, "min_confirmations", _first(
        entry.get("required_confirmations"), engine.get("required_confirmations"), 3))
    _alias(entry, "require_trend", _first(entry_trend.get("ema_alignment"), True))
    _alias(entry, "require_above_vwap", _first(entry_trend.get("price_above_vwap"), True))
    _alias(entry, "require_ema20_above_ema50", _first(entry_trend.get("ema_alignment"), True))
    _alias(entry, "breakout_lookback", _first(entry_action.get("breakout_lookback"), 20))
    _alias(entry, "min_relative_volume", _first(entry_vol.get("relative_volume_min"), 1.4))
    _alias(entry, "max_vwap_extension_pct", 2.5)
    _alias(entry, "min_atr_pct", _first(entry_volatility.get("minimum_atr_pct"), 0.4))
    _alias(entry, "max_atr_pct", _first(entry_volatility.get("maximum_atr_pct"), 3.5))
    _alias(entry, "min_risk_reward", _first(entry.get("min_risk_reward"), entry_risk.get("minimum_risk_reward"), 1.8))
    _alias(entry, "require_candle_confirmation", _first(entry_action.get("require_bullish_candle"), True))
    _alias(entry, "require_volume_confirmation", _first(entry_vol.get("require_volume_confirmation"), True))
    _alias(entry, "avoid_first_minutes", 0)
    _alias(entry_mom, "rsi_min", 55)
    _alias(entry_mom, "rsi_max", 72)
    _alias(entry_mom, "adx_min", 20)
    cfg["entry"] = entry

    short = _dict(cfg.get("short_entry"))
    if short:
        short_trend = _dict(short.get("trend"))
        short_vol = _dict(short.get("volume"))
        short_action = _dict(short.get("price_action"))
        short_volatility = _dict(short.get("volatility"))
        short_risk = _dict(short.get("risk"))
        _alias(short, "min_score", _first(short.get("minimum_score"), 65))
        _alias(short, "min_confirmations", _first(
            short.get("required_confirmations"), engine.get("required_confirmations"), 3))
        _alias(short, "require_below_vwap", _first(short_trend.get("price_below_vwap"), True))
        _alias(short, "require_below_ema20", _first(short_trend.get("ema_alignment"), True))
        _alias(short, "require_below_ema50", _first(short_trend.get("ema_alignment"), True))
        _alias(short, "require_ema20_below_ema50", _first(short_trend.get("ema_alignment"), True))
        _alias(short, "breakdown_lookback", _first(short_action.get("breakdown_lookback"), 20))
        _alias(short, "min_relative_volume", _first(short_vol.get("relative_volume_min"), 1.4))
        _alias(short, "min_atr_pct", _first(short_volatility.get("minimum_atr_pct"), 0.4))
        _alias(short, "max_atr_pct", _first(short_volatility.get("maximum_atr_pct"), 3.5))
        _alias(short, "min_risk_reward", _first(
            short.get("min_risk_reward"), short_risk.get("minimum_risk_reward"), 1.8))
        _alias(short, "require_candle_confirmation", _first(short_action.get("require_bearish_candle"), True))
        _alias(short, "require_volume_confirmation", _first(short_vol.get("require_volume_confirmation"), True))
        cfg["short_entry"] = short

    exit_rule = _dict(cfg.get("exit"))
    initial = _dict(exit_rule.get("initial_stop_loss"))
    target = _dict(exit_rule.get("target"))
    breakeven = _dict(exit_rule.get("breakeven"))
    trailing = _dict(exit_rule.get("trailing_stop"))
    time_stop = _dict(exit_rule.get("time_stop"))
    square_off = _dict(exit_rule.get("square_off"))
    overnight = _dict(exit_rule.get("overnight"))
    levels = trailing.get("profit_lock_levels") or []
    last_lock = _dict(levels[-1]) if isinstance(levels, list) and levels and isinstance(levels[-1], dict) else {}
    _alias(exit_rule, "stop_atr_mult", _first(initial.get("atr_multiplier"), 1.2))
    _alias(exit_rule, "min_stop_pct", _first(initial.get("minimum_stop_pct"), 0.4))
    _alias(exit_rule, "max_stop_pct", _first(initial.get("maximum_stop_pct"), 2.0))
    _alias(exit_rule, "target_r_mult", _first(target.get("target_r_mult"), 2.0))
    _alias(exit_rule, "trailing_atr_mult", _first(last_lock.get("atr_multiplier"), 0.8))
    _alias(exit_rule, "trail_after_r", _first(trailing.get("activate_at_r"), 1.0))
    _alias(exit_rule, "time_stop_minutes", _first(time_stop.get("maximum_holding_minutes"), 180))
    _alias(exit_rule, "move_stop_to_be_at_r", _first(breakeven.get("activate_at_r"), 1.0))
    _alias(exit_rule, "square_off", _first(square_off.get("time"), "15:20"))
    _alias(exit_rule, "force_square_off", _first(square_off.get("close_all_positions"), True))
    _alias(exit_rule, "never_carry_overnight", not bool(overnight.get("enabled", False)))
    _alias(exit_rule, "apply_to_long_and_short", True)
    cfg["exit"] = exit_rule

    scoring = _dict(cfg.get("scoring"))
    # Keep scoring.weights exactly as authored. The UI shows all seven V4
    # categories; scoring.py knows how to fold market_sector into its internal
    # quality bucket without mutating the user's document.
    _alias(scoring, "max_score", _first(scoring.get("maximum_score"), 100))
    _alias(scoring, "watchlist_score", _first(thresholds.get("watchlist"), 55))
    _alias(scoring, "qualified_score", _first(thresholds.get("qualified"), 65))
    _alias(scoring, "entry_score", _first(thresholds.get("entry"), scoring.get("minimum_trade_score"), 85))
    cfg["scoring"] = scoring

    quality = _dict(cfg.get("data_quality"))
    _alias(quality, "reject_stale_quotes", _first(validation.get("reject_stale_data"), True))
    _alias(quality, "reject_missing_ohlcv", _first(validation.get("reject_missing_ohlcv"), True))
    _alias(quality, "reject_duplicate_candles", _first(validation.get("reject_duplicate_candles"), True))
    _alias(quality, "reject_incomplete_candles", _first(validation.get("reject_invalid_prices"), True))
    _alias(quality, "stop_entries_on_data_failure", _first(validation.get("reject_invalid_prices"), True))
    _alias(quality, "require_fresh_groww_data_for_entries", bool(groww_confirmation))
    _alias(quality, "allow_yahoo_for_scanning", _first(
        fallback.get("yahoo_allowed_for_scanning"), yahoo.get("use_for_scanning"), True))
    cfg["data_quality"] = quality

    fundamentals = _dict(cfg.get("fundamentals"))
    if fundamentals:
        uses = fundamentals.get("use_for") or []
        _alias(fundamentals, "refresh_hours", _first(fundamentals.get("refresh_interval_hours"), 24))
        _alias(fundamentals, "non_blocking", not bool(fundamentals.get("blocking", False)))
        _alias(fundamentals, "use_for_quality_score", "stock_quality" in uses)
        cfg["fundamentals"] = fundamentals

    execution = _dict(cfg.get("execution"))
    safety = _dict(cfg.get("safety"))
    paper = _dict(cfg.get("paper_trading"))
    if execution or safety:
        live = _dict(execution.get("live_trading"))
        partial = _dict(execution.get("partial_fills"))
        _alias(paper, "enabled", str(execution.get("mode", "paper")).lower() == "paper")
        _alias(paper, "simulate_market_orders", _first(execution.get("simulate_market_orders"), True))
        _alias(paper, "simulate_slippage", _first(execution.get("simulate_slippage"), True))
        _alias(paper, "simulate_brokerage", _first(execution.get("simulate_brokerage"), True))
        _alias(paper, "simulate_taxes", _first(execution.get("simulate_taxes"), True))
        _alias(paper, "simulate_partial_fills", _first(partial.get("simulate"), True))
        _alias(paper, "live_order_placement", _first(live.get("allow_live_orders"), False))
        _alias(paper, "block_live_mode", _first(safety.get("live_trading_disabled"), True))
        _alias(paper, "prevent_real_orders", _first(safety.get("real_order_placement_disabled"), True))
        cfg["paper_trading"] = paper

    return cfg


def _read_yaml() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def _mtime() -> float:
    try:
        return CONFIG_PATH.stat().st_mtime
    except OSError:
        return 0.0


def load_raw_config(force: bool = False) -> dict:
    """Read the strategy document exactly as the user wrote it."""
    with _lock:
        mtime = _mtime()
        if not force and _cache["raw"] and _cache["mtime"] == mtime:
            return _cache["raw"]
        raw = _read_yaml()
        _cache["raw"] = raw
        _cache["config"] = {}
        _cache["mtime"] = mtime
        return raw


def normalize_config(data: dict) -> dict:
    """Public wrapper used by the config editor to preview a draft safely."""
    return _normalize(data)


def load_config(force: bool = False) -> dict:
    """Runtime config: raw V4 keys plus V2 aliases for existing consumers."""
    with _lock:
        mtime = _mtime()
        if not force and _cache["config"] and _cache["mtime"] == mtime:
            return _cache["config"]
        raw = load_raw_config(force=force)
        normalized = normalize_config(raw)
        _cache["config"] = normalized
        _cache["mtime"] = mtime
        return normalized


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
    days = cfg("engine", "trading_days", default=[]) or []
    if days and day.strftime("%A") not in {str(d) for d in days}:
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
    return (os.environ.get(key) or default).strip()
