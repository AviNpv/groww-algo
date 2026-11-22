"""Deterministic, provenance-carrying market snapshots for the intelligence layer.

The snapshot is a *read-only* view of the live scan, market-data health and the
paper ledger. It never places orders, never writes to the ledger and never
includes credentials or environment variables. Missing inputs are represented
explicitly (``None``/empty values) rather than raising into the engine loop.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable

from engine import ledger, marketdata, scanner, settings, strategy
from engine.intelligence.models import (
    AccountState,
    CandidateSnapshot,
    FeedState,
    MarketSnapshot,
    PositionState,
    new_id,
)


# ---------------------------------------------------------------------------
# defensive conversion helpers
# ---------------------------------------------------------------------------
def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        result = float(value)
        return result if result == result and result not in (float("inf"), float("-inf")) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None or isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _safe(fn: Callable, default: Any) -> Any:
    try:
        value = fn()
        return default if value is None else value
    except Exception:
        return default


def _cfg(*keys, default=None) -> Any:
    return _safe(lambda: settings.cfg(*keys, default=default), default)


def _now() -> dt.datetime:
    now = _safe(settings.now_ist, None)
    return now if isinstance(now, dt.datetime) else dt.datetime.now(dt.timezone.utc)


def _iso(value: Any) -> str | None:
    if isinstance(value, dt.datetime):
        return value.isoformat(timespec="seconds")
    return _text(value) if value else None


def _quote_age(as_of: Any, now: dt.datetime) -> float | None:
    if not as_of:
        return None
    try:
        stamp = dt.datetime.fromisoformat(str(as_of))
    except (TypeError, ValueError):
        return None
    try:
        zone = getattr(settings, "IST", now.tzinfo)
        if stamp.tzinfo is None and zone is not None:
            stamp = stamp.replace(tzinfo=zone)
        if stamp.tzinfo is None and now.tzinfo is not None:
            stamp = stamp.replace(tzinfo=now.tzinfo)
        return max(0.0, (now - stamp).total_seconds())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# section builders
# ---------------------------------------------------------------------------
def _market_session(now: dt.datetime) -> dict[str, Any]:
    """settings.market_session() has shipped both 0-arg and 1-arg forms."""
    raw: Any = None
    try:
        raw = settings.market_session(now)
    except TypeError:
        raw = _safe(settings.market_session, None)
    except Exception:
        raw = None

    if isinstance(raw, dict):
        result: dict[str, Any] = dict(raw)
    elif isinstance(raw, (tuple, list)):
        result = {"open": _iso(raw[0]) if len(raw) > 0 else None,
                  "close": _iso(raw[1]) if len(raw) > 1 else None}
    else:
        result = {}

    result["now"] = _iso(now)
    result.setdefault("now_ist", _iso(now))
    result.setdefault("timezone", "Asia/Kolkata")
    for key, fn in (
        ("in_entry_window", lambda: settings.in_entry_window(now)),
        ("market_open", lambda: settings.is_market_open(now)),
        ("trading_day", lambda: settings.is_trading_day(now.date())),
    ):
        try:
            result.setdefault(key, bool(fn()))
        except Exception:
            result.setdefault(key, None)
    return result


def _feed_state() -> FeedState:
    health = _dict(_safe(marketdata.health, {}))
    active = _text(health.get("active") or "unknown", "unknown")
    delayed = health.get("delayed") if "delayed" in health else None
    return FeedState(
        active=active,
        source=health.get("source") if health.get("source") is not None else health.get("active"),
        groww_ok=_bool(health.get("ok"), False) or _bool(health.get("live_data"), False),
        state=health.get("state"),
        message=health.get("message") or health.get("fallback_reason"),
        delayed=None if delayed is None else _bool(delayed),
        as_of=health.get("as_of"),
    )


def _account_state(equity: float, acc: dict, overview: dict,
                   positions: list) -> AccountState:
    cash = _num(acc.get("cash"), _num(overview.get("cash")))
    return AccountState(
        capital=_num(overview.get("starting_capital"),
                     _num(acc.get("starting_capital"),
                          _num(acc.get("initial_capital"), cash))),
        cash=cash,
        equity=_num(equity, cash),
        day_start_equity=_num(acc.get("day_start_equity"),
                              _num(overview.get("day_start_equity"), cash)),
        margin_blocked=_num(overview.get("margin_blocked")),
        open_positions=_int(overview.get("open_positions"), len(positions)),
        realized_pnl=_num(acc.get("realized_pnl"), _num(overview.get("realized_pnl"))),
    )


def _position_states(positions: list) -> list[PositionState]:
    out: list[PositionState] = []
    for raw in positions if isinstance(positions, (list, tuple)) else []:
        row = _dict(raw)
        symbol = _text(row.get("symbol"))
        if not symbol:
            continue
        out.append(PositionState(
            symbol=symbol,
            qty=_int(row.get("qty")),
            avg_price=_num(row.get("avg_price")),
            last_price=_num(row.get("last_price"), _num(row.get("avg_price"))),
            stop=row.get("stop") if row.get("stop") is not None else None,
            target=row.get("target") if row.get("target") is not None else None,
            unrealized=_num(row.get("unrealized")),
            r_multiple=row.get("r_multiple") if row.get("r_multiple") is not None else None,
        ))
    return out


def _config_baseline() -> dict[str, Any]:
    """Only non-secret strategy knobs that a report is allowed to see."""
    cfg = settings.cfg
    return {
        "mode": cfg("app", "mode", default=cfg("mode", default=None)),
        "version": cfg("app", "version", default=cfg("version", default=None)),
        "capital": cfg("account", "starting_capital",
                       default=cfg("account", "initial_capital", default=None)),
        "risk_per_trade": cfg("account", "risk_per_trade", default=None),
        "max_daily_loss": cfg("account", "max_daily_loss", default=None),
        "leverage": cfg("account", "intraday_leverage",
                        default=cfg("account", "leverage", default=None)),
        "min_score": cfg("entry", "min_score",
                         default=cfg("entry", "minimum_score", default=None)),
        "min_rr": cfg("entry", "min_risk_reward", default=None),
        "max_positions": cfg("account", "max_positions", default=None),
    }


def _candidate(row: Any, scan_ts: str, now: dt.datetime, equity: float,
               cash: float, held: set[str]) -> CandidateSnapshot | None:
    raw = _dict(row)
    symbol = _text(raw.get("symbol") or raw.get("sym"))
    if not symbol:
        return None

    entry: dict[str, Any] = {}
    try:
        entry = _dict(strategy.build_entry(dict(raw), equity, cash, held))
    except Exception as exc:  # one bad row must not invalidate the snapshot
        # Do not propagate raw exception text into prompts or stored decisions.
        entry = {"action": "SKIP", "rejections": [f"build_entry error: {type(exc).__name__}"]}

    source = _text(raw.get("source") or raw.get("data_source") or "unknown")
    quote = _dict(raw.get("quote"))
    as_of = raw.get("as_of") or raw.get("quote_as_of") or quote.get("as_of")
    as_of = _iso(as_of) if as_of else None
    age_raw = raw.get("quote_age_sec")
    age = _num(age_raw) if age_raw is not None else _quote_age(as_of, now)
    quote_age = round(age, 3) if age_raw is not None or as_of else None
    max_age = _num(_cfg("data", "validation", "max_quote_age_seconds", default=10), 10.0)

    delayed_raw = raw.get("delayed")
    stale_raw = raw.get("data_stale")
    delayed = _bool(delayed_raw, source != "groww") if delayed_raw is not None else source != "groww"
    data_stale = (_bool(stale_raw)
                  if stale_raw is not None
                  else quote_age is not None and quote_age > max_age)

    rejections = entry.get("rejections") or entry.get("rejection_reasons") or []
    if not isinstance(rejections, (list, tuple)):
        rejections = [str(rejections)]

    reasons = raw.get("reasons") or []
    if not isinstance(reasons, (list, tuple)):
        reasons = [str(reasons)]
    patterns = raw.get("patterns") or []
    if not isinstance(patterns, (list, tuple)):
        patterns = [patterns]

    def optional_float(value: Any) -> float | None:
        return None if value is None else _num(value)

    return CandidateSnapshot(
        symbol=symbol,
        price=_num(raw.get("price"), _num(entry.get("price"))),
        score=_num(raw.get("score"), _num(entry.get("score"))),
        status=_text(raw.get("status") or "UNKNOWN", "UNKNOWN"),
        confirmations=_int(raw.get("confirmations")),
        buckets=_dict(raw.get("buckets")),
        indicators=_dict(raw.get("indicators")),
        reasons=[_text(r) for r in reasons],
        patterns=list(patterns),
        source=source,
        delayed=delayed,
        data_stale=data_stale,
        quote_age_sec=quote_age,
        as_of=as_of,
        closed_candle=(None if raw.get("closed_candle") is None
                       else _bool(raw.get("closed_candle"))),
        ts=_text(raw.get("ts") or scan_ts or _iso(now)),
        action=entry.get("action"),
        stop=optional_float(entry.get("stop")),
        target=optional_float(entry.get("target")),
        qty=None if entry.get("qty") is None else _int(entry.get("qty")),
        risk_amount=optional_float(entry.get("risk_amount")),
        risk_reward=optional_float(entry.get("risk_reward")),
        rejection_reasons=[_text(r) for r in rejections],
    )


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def build_snapshot(scan_result: dict | None = None,
                   held: set[str] | None = None) -> MarketSnapshot:
    """Build a validated market snapshot from a scan result or a fresh scan."""
    now = _now()
    scan: dict = {}
    if scan_result is None:
        scan = _dict(_safe(scanner.scan, {}))
    else:
        scan = _dict(scan_result)

    results = scan.get("results") or []
    if not isinstance(results, (list, tuple)):
        results = []
    scan_ts = _text(scan.get("ts") or _iso(now))

    acc = _dict(_safe(ledger.account, {}))
    overview = _dict(_safe(ledger.overview, {}))
    equity = _num(_safe(ledger.current_equity, 0.0))
    positions = _safe(ledger.positions, [])
    if not isinstance(positions, (list, tuple)):
        positions = []
    cash = _num(acc.get("cash"), _num(overview.get("cash")))
    held_set = {str(s) for s in (held or set())}

    candidates: list[CandidateSnapshot] = []
    for row in results:
        candidate = _candidate(row, scan_ts, now, equity, cash, held_set)
        if candidate is not None:
            candidates.append(candidate)

    return MarketSnapshot(
        snapshot_id=new_id("snapshot"),
        ts=scan_ts or _iso(now) or "",
        timezone=_text(_cfg("app", "timezone", default="Asia/Kolkata"),
                       "Asia/Kolkata"),
        market_session=_market_session(now),
        feed=_feed_state(),
        account=_account_state(equity, acc, overview, positions),
        positions=_position_states(positions),
        candidates=candidates,
        config_baseline=_config_baseline(),
    )


__all__ = ["build_snapshot"]
