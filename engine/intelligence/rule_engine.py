"""Deterministic pre-entry rule engine.

Every gate is a pure check over a :class:`MarketSnapshot`, the paper ledger and
strategy.yaml. There is no model call in this module and, crucially, no call to
``broker.buy``. The result is persisted through the separate intelligence store.
"""
from __future__ import annotations

from typing import Any

from engine import broker, ledger, settings, strategy
from engine.intelligence import storage
from engine.intelligence.models import CandidateSnapshot, MarketSnapshot, RuleCheck, RuleDecision


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        result = float(value)
        return result if result == result and result not in (float("inf"), float("-inf")) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _cfg(*keys, default=None) -> Any:
    try:
        return settings.cfg(*keys, default=default)
    except Exception:
        return default


def _norm(symbol: Any) -> str:
    return str(symbol or "").strip().upper().replace(".NS", "")


def _safe(fn, default):
    try:
        value = fn()
        return default if value is None else value
    except Exception:
        return default


class RuleEngine:
    """Evaluate one candidate against the frozen V4 deterministic gates."""

    def __init__(self, store=storage) -> None:
        self.store = store

    def evaluate(self, symbol: str, snapshot: MarketSnapshot,
                 held: set[str] | None = None) -> RuleDecision:
        now = _safe(settings.now_ist, None)
        ts = now.isoformat(timespec="seconds") if now is not None else ""
        checks: list[RuleCheck] = []
        reasons: list[str] = []

        def check(name: str, ok: bool, detail: Any) -> None:
            message = str(detail)
            checks.append(RuleCheck(name=name, ok=bool(ok), detail=message))
            if not ok:
                reasons.append(f"{name}: {message}")

        candidate: CandidateSnapshot | None = next(
            (c for c in (snapshot.candidates or []) if _norm(c.symbol) == _norm(symbol)),
            None,
        )
        equity = _num(snapshot.account.equity)
        cash = _num(snapshot.account.cash)

        price = _num(candidate.price) if candidate else 0.0
        qty = int(candidate.qty or 0) if candidate else 0
        stop = _num(candidate.stop) if candidate and candidate.stop is not None else 0.0
        target = _num(candidate.target) if candidate and candidate.target is not None else 0.0
        rr = _num(candidate.risk_reward) if candidate else 0.0
        notional = round(price * qty, 2)
        computed: dict[str, Any] = {
            "symbol": _norm(symbol),
            "price": price,
            "score": _num(candidate.score) if candidate else 0.0,
            "status": candidate.status if candidate else None,
            "confirmations": int(candidate.confirmations or 0) if candidate else 0,
            "rr": rr,
            "stop": stop,
            "target": target,
            "qty": qty,
            "notional": notional,
            "risk_amount": _num(candidate.risk_amount) if candidate else 0.0,
            "source": candidate.source if candidate else None,
            "as_of": candidate.as_of if candidate else None,
            "quote_age_sec": candidate.quote_age_sec if candidate else None,
            "delayed": candidate.delayed if candidate else None,
            "data_stale": candidate.data_stale if candidate else None,
            "equity": equity,
            "cash": cash,
            "rejection_reasons": list(candidate.rejection_reasons) if candidate else [],
        }

        # (a) market session + engine permissions -----------------------------
        in_window = False
        if now is not None:
            try:
                in_window = bool(settings.in_entry_window(now))
            except Exception:
                in_window = False
        engine_enabled = bool(_cfg("engine", "enabled", default=True))
        autotrade = _cfg("engine", "autotrade",
                         default=_cfg("engine", "auto_trade", default=True))
        allow_new = bool(_cfg("engine", "allow_new_entries", default=True))
        computed.update({
            "in_entry_window": in_window,
            "engine_enabled": engine_enabled,
            "autotrade": bool(autotrade),
            "allow_new_entries": allow_new,
        })
        check(
            "market_session",
            in_window and engine_enabled and bool(autotrade) and allow_new,
            f"entry_window={in_window}, engine_enabled={engine_enabled}, "
            f"autotrade={bool(autotrade)}, allow_new_entries={allow_new}",
        )

        # (b) data freshness ---------------------------------------------------
        max_age = _num(_cfg("data", "validation", "max_quote_age_seconds", default=10), 10.0)
        computed["max_quote_age_seconds"] = max_age
        if candidate is None:
            check("candidate_present", False, f"no candidate for {symbol}")
            check("data_freshness", False, "no candidate data")
        else:
            check("candidate_present", True, "candidate found in snapshot")
            age = candidate.quote_age_sec
            freshness_problems: list[str] = []
            if candidate.data_stale:
                freshness_problems.append("data_stale")
            if str(candidate.source).strip().lower() != "groww":
                freshness_problems.append(f"source={candidate.source}")
            if candidate.delayed:
                freshness_problems.append("delayed quote")
            if age is None:
                freshness_problems.append("quote_age_sec missing")
            elif _num(age, -1.0) > max_age:
                freshness_problems.append(f"quote_age_sec={age} > {max_age:g}")
            if not candidate.as_of:
                freshness_problems.append("as_of missing")
            check("data_freshness", not freshness_problems,
                  "fresh Groww quote" if not freshness_problems
                  else ", ".join(freshness_problems))

        # (c) deterministic entry ---------------------------------------------
        rejections = list(candidate.rejection_reasons) if candidate else []
        if candidate is None:
            check("deterministic_entry", False, "no candidate")
        else:
            check(
                "deterministic_entry",
                str(candidate.action).upper() == "BUY" and not rejections,
                f"action={candidate.action}, rejections={rejections or 'none'}",
            )

        # (d) score / confirmations / status ----------------------------------
        min_score = max(85.0, _num(_cfg("entry", "min_score",
                                        default=_cfg("entry", "minimum_score", default=85)), 85.0))
        min_confirmations = max(
            5, int(_num(_cfg("entry", "min_confirmations",
                             default=_cfg("entry", "required_confirmations", default=5)), 5)),
        )
        computed.update({"min_score": min_score, "min_confirmations": min_confirmations})
        if candidate is None:
            check("entry_quality", False, "no candidate")
        else:
            quality_problems: list[str] = []
            if _num(candidate.score) < min_score:
                quality_problems.append(f"score {candidate.score} < {min_score:g}")
            if int(candidate.confirmations or 0) < min_confirmations:
                quality_problems.append(
                    f"confirmations {candidate.confirmations} < {min_confirmations}")
            if str(candidate.status).upper() != "QUALIFIED":
                quality_problems.append(f"status={candidate.status}")
            check("entry_quality", not quality_problems,
                  "score/confirmations/status qualify" if not quality_problems
                  else ", ".join(quality_problems))

        # (e) stop / target / reward:risk -------------------------------------
        min_rr = max(2.0, _num(_cfg("entry", "min_risk_reward", default=2.0), 2.0))
        computed["min_rr"] = min_rr
        if candidate is None:
            check("risk_reward", False, "no candidate")
        else:
            level_problems: list[str] = []
            if stop <= 0:
                level_problems.append("stop must be > 0")
            if not (0 < stop < price):
                level_problems.append(f"stop {stop} must be below price {price}")
            if not (target > price):
                level_problems.append(f"target {target} must be above price {price}")
            if rr < min_rr:
                level_problems.append(f"R:R {rr} < {min_rr:g}")
            check("risk_reward", not level_problems,
                  f"R:R={rr}, stop={stop}, target={target}" if not level_problems
                  else ", ".join(level_problems))

        # (f) position sizing ---------------------------------------------------
        sized = 0
        if candidate is not None and price > 0 and stop > 0:
            sized = _safe(lambda: int(broker.size_position(price, stop, equity, cash)), 0)
        computed["broker_qty"] = sized
        if candidate is None:
            check("position_size", False, "no candidate")
        else:
            size_problems: list[str] = []
            if qty < 1:
                size_problems.append(f"qty={qty}")
            if sized < 1:
                size_problems.append(f"broker.size_position={sized}")
            check("position_size", not size_problems,
                  f"qty={qty}, broker_qty={sized}" if not size_problems
                  else ", ".join(size_problems))

        # (g) exposure (per-symbol cap, consistent with max_position_pct) -------
        max_pct = _num(_cfg("account", "max_position_pct", default=45), 45.0)
        leverage = max(1.0, _num(_cfg("account", "intraday_leverage",
                                      default=_cfg("account", "leverage", default=1)), 1.0))
        exposure_cap = round(equity * max_pct / 100.0 * leverage, 2)
        ledger_positions = _safe(ledger.positions, [])
        if not isinstance(ledger_positions, (list, tuple)):
            ledger_positions = []
        target_key = _norm(symbol)
        existing = 0.0
        total_open = 0.0
        for pos in ledger_positions:
            try:
                value = _num(pos.get("value"),
                             _num(pos.get("last_price"), _num(pos.get("avg_price")))
                             * int(pos.get("qty") or 0))
            except Exception:
                value = 0.0
            total_open += value
            if _norm(pos.get("symbol")) == target_key:
                existing += value
        existing = round(existing, 2)
        total_open = round(total_open, 2)
        exposure = round(existing + notional, 2)
        computed.update({
            "max_position_pct": max_pct,
            "leverage": leverage,
            "exposure_cap": exposure_cap,
            "existing_exposure": existing,
            "total_open_market_value": total_open,
            "exposure": exposure,
        })
        check("exposure", exposure <= exposure_cap,
              f"existing {existing} + notional {notional} = {exposure} "
              f"vs cap {exposure_cap} (max_position_pct={max_pct:g}, leverage={leverage:g})")

        # (h) daily-loss / portfolio gates ------------------------------------
        gate_ok, gate_reason = True, "portfolio gates clear"
        try:
            gate_ok, gate_reason = strategy.can_open_new(equity)
            gate_ok, gate_reason = bool(gate_ok), str(gate_reason or "")
        except Exception as exc:
            gate_ok, gate_reason = False, f"can_open_new error: {type(exc).__name__}: {exc}"
        day_start = _num(snapshot.account.day_start_equity, equity)
        daily_loss = round(day_start - equity, 2)
        computed.update({"daily_loss": daily_loss, "portfolio_ok": gate_ok,
                         "portfolio_reason": gate_reason})
        check("portfolio_gates", gate_ok, gate_reason or "portfolio gate rejected")

        # (i) duplicates / per-symbol daily cap --------------------------------
        held_symbols = {_norm(s) for s in (held or set())}
        ledger_symbols = {_norm(p.get("symbol")) for p in ledger_positions}
        trade_count = _safe(lambda: int(strategy.symbol_trade_count(str(symbol))), 0)
        max_per_symbol = int(_num(_cfg("account", "max_trades_per_symbol_per_day", default=1), 1))
        computed.update({"symbol_trade_count": trade_count,
                         "max_trades_per_symbol_per_day": max_per_symbol,
                         "held": sorted(held_symbols),
                         "ledger_symbols": sorted(ledger_symbols)})
        duplicate_problems: list[str] = []
        if target_key in held_symbols:
            duplicate_problems.append("symbol is already held")
        if target_key in ledger_symbols:
            duplicate_problems.append("symbol has an open ledger position")
        if trade_count >= max_per_symbol:
            duplicate_problems.append(
                f"symbol_trade_count={trade_count} >= {max_per_symbol}")
        check("duplicate", not duplicate_problems,
              "no duplicate entry" if not duplicate_problems
              else ", ".join(duplicate_problems))

        decision = RuleDecision(
            symbol=str(symbol),
            snapshot_id=snapshot.snapshot_id,
            ts=ts,
            allowed=all(c.ok for c in checks),
            checks=checks,
            reasons=reasons,
            computed=computed,
        )
        try:
            init = getattr(self.store, "init_db", None)
            if callable(init):
                init()
            self.store.save_rule_decision(decision)
        except Exception:
            pass  # persistence must never turn an advisory decision into an exception
        return decision


__all__ = ["RuleEngine"]
