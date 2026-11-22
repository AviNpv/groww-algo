"""Structured multi-agent debate verdicts (deterministic aggregation only).

The engine never asks a model to arbitrate. It counts recommendations, applies
an uncertainty penalty and rejects whenever an unresolved critical objection is
present. Global reports (``symbol is None``) count for every symbol.
"""
from __future__ import annotations

from typing import Any, Iterable

from engine.intelligence import storage
from engine.intelligence.models import AgentReport, DebateResult


_WEIGHTS = {"support": 1.0, "abstain": 0.0, "oppose": -1.0}
_CRITICAL_ROLE_TOKENS: dict[str, tuple[str, ...]] = {
    "critic": ("critic", "adversarial", "critique"),
    "risk": ("risk",),
}
_ALL_ROLE_TOKENS: dict[str, tuple[str, ...]] = {
    "market_regime": ("marketregime", "regime", "marketanalyst"),
    "scanner": ("scanner", "stockscan"),
    "technical": ("technical", "technicals"),
    "strategy": ("strategy", "strategist"),
    **_CRITICAL_ROLE_TOKENS,
}


def _norm(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _reports(value: Any) -> list[AgentReport]:
    """Accept AgentReports, plain dicts or a wrapper with a ``reports`` field."""
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


def _identity(report: AgentReport) -> str:
    return _norm(f"{report.agent_id} {report.agent_name}")


class DebateEngine:
    """Aggregate six agent reports into a fail-closed verdict."""

    #: Roles that must always be represented before a debate can approve.
    CRITICAL_ROLE_TOKENS = _CRITICAL_ROLE_TOKENS

    def __init__(self, store=storage) -> None:
        self.store = store

    def run(self, reports: list[AgentReport], symbol: str, snapshot_id: str,
            rounds: int = 2, critical_objection_blocks: bool = True,
            min_reports: int = 6,
            required_report_ids: Iterable[str] | None = None) -> DebateResult:
        from engine.settings import now_ist

        all_reports = _reports(reports)
        target = _norm(symbol)
        selected = [r for r in all_reports
                    if r.symbol is None or _norm(r.symbol) == target]

        # Critical report presence. Explicit IDs win when supplied; otherwise
        # role tokens are checked, tolerating custom agent IDs that carry no
        # recognisable role (count is then the only available evidence).
        missing_critical: list[str] = []
        if required_report_ids is not None:
            present_ids = {_norm(r.agent_id) for r in selected}
            missing_critical = [str(i) for i in required_report_ids
                                if _norm(i) not in present_ids]
        else:
            identities = [_identity(r) for r in selected]
            matched_roles = {role for role, tokens in _ALL_ROLE_TOKENS.items()
                             if any(any(token in ident for token in tokens)
                                    for ident in identities)}
            # Critic and Risk Analyst are mandatory even when every ID is
            # unrecognised; a count of six opaque reports is not a panel.
            full_panel = int(min_reports) >= 6 and len(matched_roles) >= 4
            required_roles = (set(_ALL_ROLE_TOKENS) if full_panel
                              else set(self.CRITICAL_ROLE_TOKENS))
            missing_critical = sorted(required_roles - matched_roles)

        insufficient = (
            len(selected) < int(min_reports)
            or any(r.status != "ok" for r in selected)
            or bool(missing_critical)
        )

        unresolved = [
            objection for report in selected for objection in report.objections
            if (objection.severity == "critical" and objection.requires_resolution
                and not objection.resolved)
        ]
        flagged = [r.agent_id for r in selected if r.has_critical_objection]

        count = len(selected)
        support = sum(1 for r in selected if r.recommendation == "support")
        oppose = sum(1 for r in selected if r.recommendation == "oppose")
        abstain = sum(1 for r in selected if r.recommendation == "abstain")
        if count:
            raw = sum(_WEIGHTS.get(r.recommendation, 0.0) for r in selected) / count
            base = (raw + 1.0) / 2.0
            uncertainty = min(1.0, max(0.0,
                sum(min(1.0, max(0.0, r.uncertainty)) for r in selected) / count))
            # Half-weight uncertainty penalty: default (0.5) uncertainty still
            # allows a unanimous, well-evidenced panel to clear 0.66, while a
            # fully uncertain panel cannot.
            consensus = round(min(1.0, max(0.0, base * (1.0 - 0.5 * uncertainty))), 4)
        else:
            consensus = 0.0

        if insufficient:
            decision = "insufficient_evidence"
        elif flagged or (critical_objection_blocks and unresolved):
            # A report's own has_critical_objection flag is a hard stop even if
            # debate aggregation was configured to allow unresolved objections.
            decision = "reject_candidate"
        elif consensus < 0.66:
            decision = "reject_candidate"
        elif insufficient:  # mirrored spec ordering; keeps the gate fail-closed
            decision = "insufficient_evidence"
        else:
            decision = "approve_candidate"

        parts = [f"{rounds} round(s)", f"{count} report(s)",
                 f"support={support}", f"oppose={oppose}", f"abstain={abstain}",
                 f"consensus={consensus:.3f}"]
        if missing_critical:
            parts.append("missing critical reports: " + ", ".join(missing_critical))
        if unresolved:
            parts.append("unresolved critical objections: " + "; ".join(
                f"{o.category}: {o.message}" for o in unresolved[:4]))
        if flagged:
            parts.append("critical objection flagged by: " + ", ".join(flagged))
        if insufficient and not missing_critical:
            statuses = ", ".join(f"{r.agent_id}={r.status}" for r in selected) or "none"
            parts.append(f"insufficient evidence ({statuses})")
        rationale = f"Debate {decision}: " + "; ".join(parts) + "."

        try:
            ts = now_ist().isoformat(timespec="seconds")
        except Exception:
            ts = ""
        result = DebateResult(
            symbol=str(symbol),
            snapshot_id=str(snapshot_id),
            ts=ts,
            rounds=int(rounds),
            reports=selected,
            unresolved_critical=list(unresolved),
            consensus_score=consensus,
            decision=decision,  # type: ignore[arg-type]
            rationale=rationale,
        )
        try:
            init = getattr(self.store, "init_db", None)
            if callable(init):
                init()
            self.store.save_debate(result)
        except Exception:
            pass
        return result


__all__ = ["DebateEngine"]
