"""Trade-decision orchestrator: the single advisory gate before a paper buy.

The orchestrator composes the deterministic snapshot, the six agent reports,
the debate verdict and the rule engine. It is paper-only, persists every
decision, and never raises into the engine loop.
"""
from __future__ import annotations

from typing import Any

from engine.intelligence import config as ai_config
from engine.intelligence import storage
from engine.intelligence.debate import DebateEngine
from engine.intelligence.models import (
    AgentReport,
    CandidateSnapshot,
    ExecutionLog,
    MarketSnapshot,
    TradeDecision,
)
from engine.intelligence.rule_engine import RuleEngine
from engine.intelligence.snapshot import build_snapshot


DISABLED_RATIONALE = "intelligence disabled; existing deterministic path unchanged"
ERROR_REASON = "intelligence_evaluation_error"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        result = float(value)
        return result if result == result and result not in (float("inf"), float("-inf")) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _now_iso() -> str:
    try:
        from engine.settings import now_ist
        return now_ist().isoformat(timespec="seconds")
    except Exception:
        return ""


def _norm(value: Any) -> str:
    return str(value or "").strip().upper().replace(".NS", "")


def _reports(value: Any) -> list[AgentReport]:
    if value is None:
        return []
    if isinstance(value, AgentReport):
        return [value]
    if isinstance(value, dict):
        if "reports" in value:
            return _reports(value["reports"])
        out: list[AgentReport] = []
        for item in value.values():
            out.extend(_reports(item))
        return out
    if isinstance(value, (list, tuple, set)):
        out: list[AgentReport] = []
        for item in value:
            out.extend(_reports(item))
        return out
    if hasattr(value, "reports"):
        return _reports(getattr(value, "reports"))
    return []


def _default_agents():
    try:
        from engine.intelligence import agents as agents_module
        return agents_module
    except Exception:
        return None


class Orchestrator:
    """Evaluate one candidate signal into a persisted :class:`TradeDecision`."""

    def __init__(self, agents=None, rules=None, debate=None, store=storage) -> None:
        self.agents = agents
        self.rules = rules
        self.debate = debate
        self.store = store or storage

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    @staticmethod
    def _last_scan() -> dict | None:
        """Reuse the engine's most recent scan instead of paying for another."""
        try:
            from engine.engine import ENGINE
            scan = getattr(ENGINE, "last_scan", None)
            return scan if isinstance(scan, dict) else None
        except Exception:
            return None

    def _run_agents(self, snapshot: MarketSnapshot,
                    candidate: CandidateSnapshot) -> list[AgentReport]:
        runner_obj = self.agents if self.agents is not None else _default_agents()
        if runner_obj is None:
            return []
        runner = getattr(runner_obj, "run", None) or getattr(runner_obj, "run_agents", None)
        if not callable(runner):
            return []
        try:
            result = runner(snapshot, [candidate])
        except TypeError:
            try:
                result = runner(snapshot=snapshot, candidates=[candidate])
            except Exception:
                return []
        except Exception:
            return []
        return _reports(result)

    def _run_debate(self, reports: list[AgentReport], symbol: str,
                    snapshot: MarketSnapshot):
        engine = self.debate
        if engine is None:
            engine = DebateEngine(store=self.store)
        elif isinstance(engine, type):
            try:
                engine = engine(store=self.store)
            except Exception:
                engine = engine()
        run = getattr(engine, "run", None)
        if not callable(run):
            # A pre-computed result may be injected by tests/callers.
            return engine if hasattr(engine, "decision") else None
        cfg = ai_config.debate()
        try:
            from engine.intelligence.prompts import AGENT_IDS
            required_ids = list(AGENT_IDS)
        except Exception:
            required_ids = []
        try:
            return run(
                reports,
                symbol,
                snapshot.snapshot_id,
                rounds=int(cfg.get("rounds", 2) or 2),
                critical_objection_blocks=bool(cfg.get("critical_objection_blocks", True)),
                min_reports=int(cfg.get("min_reports", 6) or 6),
                required_report_ids=required_ids,
            )
        except TypeError:
            try:
                return run(reports, symbol, snapshot.snapshot_id)
            except Exception:
                return None
        except Exception:
            return None

    def _run_rules(self, symbol: str, snapshot: MarketSnapshot, held: set[str] | None):
        engine = self.rules
        if engine is None:
            engine = RuleEngine(store=self.store)
        elif isinstance(engine, type):
            try:
                engine = engine(store=self.store)
            except Exception:
                engine = engine()
        evaluate = getattr(engine, "evaluate", None)
        if not callable(evaluate):
            # A pre-computed result may be injected by tests/callers.
            return engine if hasattr(engine, "allowed") else None
        try:
            return evaluate(symbol, snapshot, held)
        except Exception:
            return None

    def _persist(self, decision: TradeDecision) -> None:
        try:
            init = getattr(self.store, "init_db", None)
            if callable(init):
                init()
        except Exception:
            pass
        try:
            self.store.save_trade_decision(decision)
        except Exception:
            pass
        try:
            self.store.save_execution_log(ExecutionLog(
                ts=decision.ts or _now_iso(),
                symbol=decision.symbol,
                event="intelligence_decision",
                decision_id=decision.decision_id,
                detail=f"{decision.action}: {decision.rationale}",
                paper_only=True,
            ))
        except Exception:
            pass

    @staticmethod
    def _candidate_from_signal(signal: dict, snapshot: MarketSnapshot) -> CandidateSnapshot | None:
        """Last-resort view of a caller-supplied signal; never fabricates a BUY."""
        if not signal.get("symbol") or signal.get("price") is None:
            return None
        return CandidateSnapshot(
            symbol=str(signal["symbol"]),
            price=_num(signal.get("price")),
            score=_num(signal.get("score")),
            status=str(signal.get("status") or "UNKNOWN"),
            confirmations=int(_num(signal.get("confirmations"))),
            source=str(signal.get("source") or "unknown"),
            delayed=bool(signal.get("delayed", True)),
            data_stale=bool(signal.get("data_stale", True)),
            quote_age_sec=(None if signal.get("quote_age_sec") is None
                           else _num(signal.get("quote_age_sec"))),
            as_of=signal.get("as_of"),
            closed_candle=(None if signal.get("closed_candle") is None
                           else bool(signal.get("closed_candle"))),
            ts=snapshot.ts,
            action=(None if signal.get("action") is None else str(signal.get("action"))),
            stop=(None if signal.get("stop") is None else _num(signal.get("stop"))),
            target=(None if signal.get("target") is None else _num(signal.get("target"))),
            qty=(None if signal.get("qty") is None else int(_num(signal.get("qty")))),
            risk_reward=(None if signal.get("risk_reward") is None
                         else _num(signal.get("risk_reward"))),
            rejection_reasons=[str(r) for r in (signal.get("rejections") or [])],
        )

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def evaluate(self, signal: dict, snapshot: MarketSnapshot | None = None,
                 held: set[str] | None = None) -> TradeDecision:
        signal = signal if isinstance(signal, dict) else {}
        symbol = str(signal.get("symbol") or "")
        snap = snapshot if isinstance(snapshot, MarketSnapshot) else None
        snapshot_id = (snap.snapshot_id if snap is not None
                       else str(signal.get("snapshot_id") or "disabled"))
        decision: TradeDecision | None = None

        try:
            # Disabled: the pre-existing deterministic path is byte-for-byte
            # unchanged. The decision is still recorded for observability.
            if not ai_config.enabled():
                decision = TradeDecision(
                    symbol=symbol, action="ALLOW", ts=_now_iso(),
                    snapshot_id=snapshot_id, rationale=DISABLED_RATIONALE,
                    model_ok=False, reject_reasons=[],
                )
            else:
                if snap is None:
                    snap = build_snapshot(scan_result=self._last_scan(), held=held)
                    snapshot_id = snap.snapshot_id
                candidate = next(
                    (c for c in snap.candidates if _norm(c.symbol) == _norm(symbol)),
                    None,
                )
                if candidate is None:
                    candidate = self._candidate_from_signal(signal, snap)

                if candidate is None:
                    reject_reasons = [f"candidate not found in snapshot for {symbol}"]
                    decision = TradeDecision(
                        symbol=symbol, action="REJECT", ts=_now_iso(),
                        snapshot_id=snapshot_id,
                        rationale="intelligence gates failed: " + "; ".join(reject_reasons),
                        model_ok=False, reject_reasons=reject_reasons,
                    )
                else:
                    reports = self._run_agents(snap, candidate)
                    model_ok = bool(reports) and all(r.status == "ok" for r in reports)
                    debate = self._run_debate(reports, symbol, snap)
                    rules = self._run_rules(symbol, snap, held)
                    unresolved = list(debate.unresolved_critical) if debate else []
                    flagged = [r.agent_id for r in reports if r.has_critical_objection]
                    reject_reasons: list[str] = []

                    if not model_ok:
                        if not reports:
                            reject_reasons.append("no agent reports available")
                        else:
                            reject_reasons.extend(
                                f"{r.agent_id}: status={r.status}" for r in reports
                                if r.status != "ok"
                            )
                    if debate is None:
                        reject_reasons.append("debate unavailable")
                    elif debate.decision != "approve_candidate":
                        reject_reasons.append(f"debate={debate.decision}: {debate.rationale}")
                    if unresolved:
                        reject_reasons.extend(
                            f"unresolved critical objection: {o.message}" for o in unresolved)
                    if flagged:
                        reject_reasons.append(
                            "critical objection flagged by: " + ", ".join(flagged))
                    if rules is None:
                        reject_reasons.append("rule decision unavailable")
                    elif not rules.allowed:
                        reject_reasons.extend(rules.reasons or ["deterministic rules rejected"])

                    fail_closed = bool(ai_config.fail_closed())
                    complete = bool(reports) and model_ok and debate is not None and rules is not None
                    fail_closed_ok = (not fail_closed) or (
                        complete and not unresolved and not flagged)

                    allowed = (
                        rules is not None and rules.allowed
                        and debate is not None and debate.decision == "approve_candidate"
                        and model_ok and not unresolved and not flagged and fail_closed_ok
                    )
                    rationale = (
                        "intelligence gates satisfied: rules allowed, debate approved, "
                        "model reports ok, no critical objections"
                        if allowed else
                        "intelligence gates failed: " + "; ".join(reject_reasons)
                    )
                    decision = TradeDecision(
                        symbol=symbol,
                        action="ALLOW" if allowed else "REJECT",
                        ts=_now_iso(),
                        snapshot_id=snapshot_id,
                        rationale=rationale,
                        debate=debate,
                        rules=rules,
                        model_ok=model_ok,
                        reject_reasons=[] if allowed else _dedupe(reject_reasons),
                    )
        except Exception as exc:
            # Never propagate into AlgoEngine.run_scan(). An evaluation error
            # always rejects a NEW entry; only the explicit disabled path above
            # is allowed to defer to the original deterministic behaviour.
            reason = f"{ERROR_REASON}: {type(exc).__name__}"
            decision = TradeDecision(
                symbol=symbol, action="REJECT", ts=_now_iso(),
                snapshot_id=snapshot_id, rationale=reason,
                model_ok=False, reject_reasons=[reason],
            )

        if decision is None:  # defensive; should be unreachable
            decision = TradeDecision(
                symbol=symbol, action="REJECT", ts=_now_iso(),
                snapshot_id=snapshot_id, rationale=ERROR_REASON,
                model_ok=False, reject_reasons=[ERROR_REASON],
            )
        self._persist(decision)
        return decision

    def evaluate_all(self, snapshot: MarketSnapshot | None = None,
                     held: set[str] | None = None) -> list[TradeDecision]:
        """Convenience batch evaluation of every candidate in a snapshot."""
        snap = snapshot if isinstance(snapshot, MarketSnapshot) else None
        if snap is None:
            snap = build_snapshot(scan_result=self._last_scan(), held=held)
        return [self.evaluate({"symbol": c.symbol}, snapshot=snap, held=held)
                for c in snap.candidates]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _require_for_entries() -> bool:
    try:
        return bool(ai_config.require_for_entries())
    except Exception:
        return True


# Used by engine._intelligence_gate(); created without importing agents.
ORCHESTRATOR = Orchestrator()


__all__ = ["Orchestrator", "ORCHESTRATOR"]
