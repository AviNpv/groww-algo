"""Six independent advisory agents producing schema-valid AgentReports."""
from __future__ import annotations

import datetime as dt
import threading

from engine.intelligence import prompts
from engine.intelligence import storage
from engine.intelligence.model_adapter import ModelAdapter
from engine.intelligence.models import (
    AgentReport,
    CandidateSnapshot,
    MarketSnapshot,
    Objection,
)
from engine.settings import now_ist


def _timestamp() -> str:
    try:
        return now_ist().isoformat(timespec="seconds")
    except Exception:  # pragma: no cover - defensive only
        return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def error_report(
    snapshot: MarketSnapshot,
    agent_id: str,
    symbol: str | None = None,
    reason: str = "model call failed",
    model: str | None = None,
    usage_id: str | None = None,
) -> AgentReport:
    """Deterministic fail-closed report used whenever an agent cannot answer."""
    name = prompts.AGENT_NAMES.get(agent_id, agent_id)
    detail = str(reason or "model call failed").strip()[:300] or "model call failed"
    return AgentReport(
        agent_id=agent_id,
        agent_name=name,
        symbol=symbol,
        snapshot_id=snapshot.snapshot_id,
        ts=_timestamp(),
        status="error",
        summary=(
            f"{name} could not complete model analysis: {detail}. "
            "No new evidence was evaluated and abstention is required."
        ),
        evidence=[],
        missing_information=["Model analysis unavailable; no evidence was evaluated."],
        uncertainty=1.0,
        confidence=0.0,
        objections=[
            Objection(
                id=f"obj-{agent_id}-{snapshot.snapshot_id}-{symbol or 'global'}",
                severity="critical",
                category="model_failure",
                message=f"{name} unavailable: {detail}",
                evidence_refs=[],
                requires_resolution=True,
                resolved=False,
            )
        ],
        recommendation="abstain",
        model=model,
        usage_id=usage_id,
    )


class RuntimeAgents:
    """Run every agent for every supplied candidate, independently."""

    def __init__(self, adapter: ModelAdapter | None = None, store=storage) -> None:
        self.adapter = adapter if adapter is not None else ModelAdapter()
        self.store = store

    def run(
        self,
        snapshot: MarketSnapshot,
        candidates: list[CandidateSnapshot] | None = None,
    ) -> list[AgentReport]:
        """Return six reports per candidate, or six global reports if none."""
        targets: list[CandidateSnapshot | None] = list(candidates) if candidates else [None]
        reports: list[AgentReport] = []
        for candidate in targets:
            symbol = candidate.symbol if candidate is not None else None
            for agent_id in prompts.AGENT_IDS:
                reports.append(self._run_one(snapshot, candidate, agent_id, symbol))
        return reports

    # ----------------------------------------------------------------- internals
    def _run_one(
        self,
        snapshot: MarketSnapshot,
        candidate: CandidateSnapshot | None,
        agent_id: str,
        symbol: str | None,
    ) -> AgentReport:
        model: str | None = None
        usage_id: str | None = None
        payload: dict = {}
        try:
            payload = prompts.build_user_payload(snapshot, candidate, agent_id)
            result = self.adapter.complete(
                AgentReport,
                prompts.SYSTEM_PROMPTS[agent_id],
                payload,
                agent_id,
                correlation_id=snapshot.snapshot_id,
            )
            model = getattr(getattr(result, "usage", None), "model", None)
            usage_id = getattr(getattr(result, "usage", None), "usage_id", None)
            if not result.ok or result.data is None:
                detail = str(result.error or "model call failed")
                return self._save(error_report(snapshot, agent_id, symbol, detail, model, usage_id))
            report = AgentReport.model_validate(result.data)
        except Exception as exc:
            detail = f"{type(exc).__name__}: agent report unavailable"
            return self._save(error_report(snapshot, agent_id, symbol, detail, model, usage_id))

        # Runtime-owned fields are never trusted from model output.
        report.agent_id = agent_id
        report.agent_name = prompts.AGENT_NAMES.get(agent_id, agent_id)
        report.symbol = symbol
        report.snapshot_id = snapshot.snapshot_id
        report.ts = _timestamp()
        report.status = "ok" if report.evidence else "insufficient_data"
        report.model = model
        report.usage_id = usage_id
        self._validate_refs(report, set(payload.get("allowed_evidence_refs") or []))
        return self._save(report)

    @staticmethod
    def _validate_refs(report: AgentReport, allowed: set[str]) -> None:
        """Drop evidence whose ref is not present in the payload sent to the model."""
        kept = []
        dropped: list[str] = []
        for item in report.evidence:
            ref = str(item.ref or "").strip()
            if ref and ref in allowed:
                item.ref = ref
                kept.append(item)
            else:
                dropped.append(ref or "<missing>")
        report.evidence = kept
        for ref in dict.fromkeys(dropped):
            note = (
                f"Evidence reference '{ref[:120]}' was not present in the supplied "
                "snapshot payload and was discarded."
            )
            if note not in report.missing_information:
                report.missing_information.append(note)
        for objection in report.objections:
            objection.evidence_refs = [
                str(ref) for ref in objection.evidence_refs if str(ref) in allowed
            ]

    def _save(self, report: AgentReport) -> AgentReport:
        try:
            self.store.save_report(report)
        except Exception:
            pass  # persistence failure must not break the advisory run
        return report


_DEFAULT_RUNNER: RuntimeAgents | None = None
_DEFAULT_LOCK = threading.Lock()


def run(
    snapshot: MarketSnapshot,
    candidates: list[CandidateSnapshot] | None = None,
) -> list[AgentReport]:
    """Module-level convenience wrapper for callers using the default adapter."""
    global _DEFAULT_RUNNER
    if _DEFAULT_RUNNER is None:
        with _DEFAULT_LOCK:
            if _DEFAULT_RUNNER is None:
                _DEFAULT_RUNNER = RuntimeAgents()
    return _DEFAULT_RUNNER.run(snapshot, candidates)
