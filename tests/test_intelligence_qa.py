"""Independent QA suite for the runtime intelligence layer.

Scope and safety
----------------
* No implementation file or configuration is modified by this suite.
* Every intelligence-store write is redirected to a temporary SQLite file by
  monkeypatching ``settings.cfg`` exactly as seen by ``engine.intelligence.storage``.
  ``data/algo.sqlite`` is never opened by these tests.
* Tests that exercise promotion/rollback patch ``_atomic_write_strategy`` (or a
  lower-level write helper) so ``config/strategy.yaml`` is never written.
* Former ``@unittest.expectedFailure`` cases are now normal passing tests after
  remediation; the original defect history is in ``docs/ai-testing-report.md``.

Run with:
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_intelligence_qa -v
"""
from __future__ import annotations

import ast
import copy
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import unittest
import urllib.error
import warnings
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml
from fastapi import HTTPException
from pydantic import BaseModel

from engine import api as engine_api
from engine import settings
from engine.intelligence import api as intelligence_api
from engine.intelligence import config as ai_config
from engine.intelligence import evolution as evolution_module
from engine.intelligence import model_adapter as adapter_module
from engine.intelligence import orchestrator as orchestrator_module
from engine.intelligence import rule_engine as rules_module
from engine.intelligence import storage
from engine.intelligence.debate import DebateEngine
from engine.intelligence.models import (
    AccountState,
    AgentReport,
    CandidateSnapshot,
    DebateResult,
    ExecutionLog,
    FeedState,
    MarketSnapshot,
    Objection,
    RuleDecision,
    StrategyVersion,
)
from engine.intelligence.orchestrator import DISABLED_RATIONALE, Orchestrator
from engine.intelligence.rule_engine import RuleEngine

# The main app uses FastAPI's deprecated on_event hooks; that is out of scope here.
warnings.filterwarnings("ignore", message="on_event is deprecated",
                        category=DeprecationWarning)

REPO = Path(__file__).resolve().parents[1]
AGENT_IDS = (
    "market_regime", "stock_scanner", "technical_analyst",
    "strategy_analyst", "adversarial_critic", "risk_analyst",
)
QA_TS = "2026-11-22T10:00:00+05:30"


# ---------------------------------------------------------------------------
# shared builders
# ---------------------------------------------------------------------------
def make_candidate(**overrides: Any) -> CandidateSnapshot:
    data: dict[str, Any] = {
        "symbol": "TEST", "price": 100.0, "score": 90.0, "status": "QUALIFIED",
        "confirmations": 5, "source": "groww", "delayed": False,
        "data_stale": False, "quote_age_sec": 1.0,
        "as_of": "2026-11-22T09:59:55+05:30", "closed_candle": True,
        "ts": QA_TS, "action": "BUY", "stop": 95.0, "target": 110.0,
        "qty": 5, "risk_amount": 25.0, "risk_reward": 2.0,
        "rejection_reasons": [],
    }
    data.update(overrides)
    return CandidateSnapshot(**data)


def make_snapshot(candidate: CandidateSnapshot | None = None,
                  account: dict | None = None) -> MarketSnapshot:
    acc = {
        "capital": 10000.0, "cash": 10000.0, "equity": 10000.0,
        "day_start_equity": 10000.0, "margin_blocked": 0.0,
        "open_positions": 0, "realized_pnl": 0.0,
    }
    acc.update(account or {})
    return MarketSnapshot(
        snapshot_id="snap-qa", ts=QA_TS,
        feed=FeedState(active="groww", source="groww", groww_ok=True, delayed=False),
        account=AccountState(**acc), positions=[],
        candidates=[candidate if candidate is not None else make_candidate()],
        config_baseline={"mode": "paper", "min_score": 85, "min_rr": 2.0},
    )


def make_report(agent_id: str, **overrides: Any) -> AgentReport:
    data: dict[str, Any] = {
        "agent_id": agent_id,
        "agent_name": agent_id.replace("_", " ").title(),
        "symbol": "TEST", "snapshot_id": "snap-qa", "ts": QA_TS,
        "summary": "QA report", "status": "ok", "recommendation": "support",
        "uncertainty": 0.1, "confidence": 0.9,
    }
    data.update(overrides)
    return AgentReport(**data)


def make_panel(**per_agent: dict) -> list[AgentReport]:
    return [make_report(agent_id, **(per_agent.get(agent_id) or {}))
            for agent_id in AGENT_IDS]


def make_objection(category: str = "data", message: str = "unresolved objection",
                   resolved: bool = False, requires_resolution: bool = True) -> Objection:
    return Objection(
        severity="critical", category=category, message=message,
        requires_resolution=requires_resolution, resolved=resolved,
    )


def make_debate(decision: str = "approve_candidate") -> DebateResult:
    return DebateResult(
        symbol="TEST", snapshot_id="snap-qa", ts=QA_TS, rounds=2,
        reports=make_panel(), consensus_score=0.9, decision=decision,  # type: ignore[arg-type]
        rationale=f"synthetic {decision}",
    )


def set_path(data: dict, path: tuple[str, ...], value: Any) -> None:
    node = data
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def dotted(path: tuple[str, ...]) -> str:
    return ".".join(path)


# ---------------------------------------------------------------------------
# scratch-store base class
# ---------------------------------------------------------------------------
class ScratchDBTest(unittest.TestCase):
    """Patch the storage path only; production DB/config are left untouched."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "qa-intelligence.sqlite")
        self.cfg_overrides: dict[tuple[str, ...], Any] = {
            ("intelligence", "storage", "db_path"): self.db_path,
        }
        real_cfg = settings.cfg
        state = self

        def fake_cfg(*keys: str, default: Any = None) -> Any:
            if keys in state.cfg_overrides:
                return state.cfg_overrides[keys]
            return real_cfg(*keys, default=default)

        self._cfg_patcher = patch.object(settings, "cfg", side_effect=fake_cfg)
        self._cfg_patcher.start()
        storage.init_db()

    def tearDown(self) -> None:
        self._cfg_patcher.stop()
        self._tmp.cleanup()

    def override_cfg(self, keys: tuple[str, ...], value: Any) -> None:
        self.cfg_overrides[keys] = value


class _SpyAgents:
    def __init__(self, reports: list[AgentReport] | None = None,
                 exc: Exception | None = None) -> None:
        self.reports = list(reports or [])
        self.exc = exc
        self.calls = 0

    def run(self, snapshot: Any, candidates: Any = None, **kwargs: Any) -> list[AgentReport]:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return list(self.reports)


class _SpyDebate:
    def __init__(self, result: DebateResult | None) -> None:
        self.result = result
        self.calls = 0

    def run(self, *args: Any, **kwargs: Any) -> DebateResult | None:
        self.calls += 1
        return self.result


class _SpyRules:
    def __init__(self, allowed: bool = True, reasons: list[str] | None = None) -> None:
        self.allowed = allowed
        self.reasons = list(reasons or [])
        self.calls = 0

    def evaluate(self, symbol: str, snapshot: Any, held: Any = None) -> RuleDecision:
        self.calls += 1
        return RuleDecision(
            symbol=symbol, snapshot_id=getattr(snapshot, "snapshot_id", "snap-qa"),
            ts=QA_TS, allowed=self.allowed, reasons=self.reasons,
            checks=[], computed={"synthetic": True},
        )


class _UsageStore:
    def __init__(self) -> None:
        self.usages: list[Any] = []

    def save_model_usage(self, usage: Any) -> None:
        self.usages.append(usage)


class _SpyEnviron(dict):
    """Environment double that records every env-var read."""

    def __init__(self, values: dict[str, str]) -> None:
        super().__init__(values)
        self.get_calls: list[str] = []

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        self.get_calls.append(key)
        return super().get(key, default)


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False

    def read(self, amount: int = -1) -> bytes:
        if amount is None or amount < 0:
            return self._payload
        return self._payload[:amount]


class _UrlopenCapture:
    def __init__(self, payload: bytes = b"{}",
                 exc: Exception | None = None) -> None:
        self.payload = payload
        self.exc = exc
        self.calls: list[tuple[Any, Any]] = []

    def __call__(self, request: Any, timeout: Any = None) -> _FakeResponse:
        self.calls.append((request, timeout))
        if self.exc is not None:
            raise self.exc
        return _FakeResponse(self.payload)


class _QAOutput(BaseModel):
    verdict: str
    score: int


class _MemoryStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.logs: list[Any] = []

    def init_db(self) -> None:
        return None

    def save_version(self, version: StrategyVersion) -> None:
        self.rows[version.version_id] = {
            "id": version.version_id, "ts": version.ts, "status": version.status,
            "yaml": version.yaml, "params": dict(version.params),
            "metrics": dict(version.metrics),
            "parent_version_id": version.parent_version_id,
            "rationale": version.rationale,
        }

    def get_version(self, version_id: str) -> dict | None:
        row = self.rows.get(version_id)
        return dict(row) if row else None

    def set_version_status(self, version_id: str, status: str) -> None:
        if version_id in self.rows:
            self.rows[version_id]["status"] = status

    def active_version(self) -> dict | None:
        actives = [dict(r) for r in self.rows.values() if r["status"] == "active"]
        return max(actives, key=lambda r: r["ts"]) if actives else None

    def versions(self, limit: int = 30) -> list[dict]:
        return [dict(r) for r in sorted(
            self.rows.values(), key=lambda r: r["ts"], reverse=True)[:limit]]

    def save_execution_log(self, entry: ExecutionLog) -> None:
        self.logs.append(entry)


# ---------------------------------------------------------------------------
# 1. Debate
# ---------------------------------------------------------------------------
class DebateQATests(ScratchDBTest):
    def _run(self, reports: list[AgentReport],
             critical_objection_blocks: bool = True) -> Any:
        return DebateEngine(store=storage).run(
            reports, "TEST", "snap-qa", rounds=2,
            critical_objection_blocks=critical_objection_blocks,
            min_reports=6,
        )

    def test_critic_unresolved_critical_objection_rejects(self):
        reports = make_panel(**{
            "adversarial_critic": {"objections": [
                make_objection("adversarial", "stale quote contradicts signal")]},
        })
        result = self._run(reports)
        self.assertEqual(result.decision, "reject_candidate")
        self.assertEqual(len(result.unresolved_critical), 1)
        self.assertEqual(result.unresolved_critical[0].category, "adversarial")
        self.assertIn("unresolved critical objections", result.rationale)

    def test_critic_objection_blocks_even_when_aggregation_flag_is_off(self):
        reports = make_panel(**{
            "adversarial_critic": {"objections": [
                make_objection("adversarial", "contradictory evidence")]},
        })
        result = self._run(reports, critical_objection_blocks=False)
        self.assertEqual(result.decision, "reject_candidate")
        self.assertIn("critical objection flagged by", result.rationale)

    def test_risk_analyst_unresolved_critical_objection_rejects(self):
        reports = make_panel(**{
            "risk_analyst": {"objections": [
                make_objection("risk", "exposure exceeds limit")]},
        })
        result = self._run(reports)
        self.assertEqual(result.decision, "reject_candidate")
        self.assertEqual(result.unresolved_critical[0].category, "risk")

    def test_risk_objection_blocks_even_when_aggregation_flag_is_off(self):
        reports = make_panel(**{
            "risk_analyst": {"objections": [make_objection("risk", "unquantified downside")]},
        })
        result = self._run(reports, critical_objection_blocks=False)
        self.assertEqual(result.decision, "reject_candidate")

    def test_resolved_or_nonblocking_objection_does_not_reject(self):
        reports = make_panel(**{
            "adversarial_critic": {"objections": [
                make_objection("adversarial", "handled", resolved=True)]},
            "risk_analyst": {"objections": [
                make_objection("risk", "informational",
                               requires_resolution=False)]},
        })
        result = self._run(reports)
        self.assertEqual(result.decision, "approve_candidate")
        self.assertEqual(result.unresolved_critical, [])

    def test_missing_reports_report_insufficient_evidence(self):
        for label, reports in (("empty", []), ("five", make_panel()[:5])):
            with self.subTest(label=label):
                result = self._run(reports)
                self.assertEqual(result.decision, "insufficient_evidence")

    def test_missing_critical_role_reports_insufficient_evidence(self):
        reports = make_panel()[:5] + [make_report("unrecognised_extra",
                                                  agent_name="Extra Observer")]
        result = self._run(reports)
        self.assertEqual(result.decision, "insufficient_evidence")
        self.assertIn("missing critical reports", result.rationale)
        self.assertTrue(any("critic" in token for token in result.rationale.split()))

    def test_error_status_report_is_insufficient_evidence(self):
        reports = make_panel(**{"market_regime": {"status": "error"}})
        result = self._run(reports)
        self.assertEqual(result.decision, "insufficient_evidence")

    def test_debate_is_persisted_to_scratch_store_only(self):
        self._run(make_panel())
        latest = storage.latest_debate("TEST")
        self.assertIsNotNone(latest)
        self.assertEqual(latest["decision"], "approve_candidate")
        self.assertEqual(Path(storage.db_path()).resolve(), Path(self.db_path).resolve())

    def test_unrecognised_agent_ids_cannot_satisfy_critical_roles(self):
        """Six opaque reports cannot stand in for Critic and Risk Analyst."""
        reports = [make_report(f"agent_{i}", agent_name=f"Agent {i}")
                   for i in range(6)]
        result = self._run(reports)
        self.assertEqual(result.decision, "insufficient_evidence")


# ---------------------------------------------------------------------------
# 2. Rule engine
# ---------------------------------------------------------------------------
class RuleEngineQATests(ScratchDBTest):
    def setUp(self) -> None:
        super().setUp()
        now = dt.datetime(2026, 11, 22, 10, 0, tzinfo=settings.IST)
        self.enterContext(patch.object(rules_module.settings, "now_ist",
                                       return_value=now))
        self.in_window = self.enterContext(patch.object(
            rules_module.settings, "in_entry_window", return_value=True))
        self.enterContext(patch.object(rules_module.broker, "size_position",
                                       return_value=5))
        self.enterContext(patch.object(rules_module.ledger, "positions",
                                       return_value=[]))
        self.can_open = self.enterContext(patch.object(
            rules_module.strategy, "can_open_new",
            return_value=(True, "portfolio gates clear")))
        self.enterContext(patch.object(rules_module.strategy,
                                       "symbol_trade_count", return_value=0))

    def _evaluate(self, candidate: CandidateSnapshot | None = None,
                  account: dict | None = None, held: set[str] | None = None):
        return RuleEngine(store=storage).evaluate(
            "TEST", make_snapshot(candidate, account), held=held)

    @staticmethod
    def _checks(decision):
        return {check.name: check for check in decision.checks}

    def test_baseline_candidate_is_allowed(self):
        decision = self._evaluate()
        self.assertTrue(decision.allowed, decision.reasons)
        self.assertTrue(all(check.ok for check in decision.checks))
        self.assertEqual(decision.computed["min_score"], 85.0)
        self.assertEqual(decision.computed["min_rr"], 2.0)

    def test_stale_data_is_rejected(self):
        for label, kwargs in (
            ("data_stale", {"data_stale": True}),
            ("old_quote", {"quote_age_sec": 120.0}),
            ("missing_as_of", {"as_of": None}),
        ):
            with self.subTest(label=label):
                decision = self._evaluate(make_candidate(**kwargs))
                checks = self._checks(decision)
                self.assertFalse(decision.allowed)
                self.assertFalse(checks["data_freshness"].ok, checks)
                self.assertIn("data_freshness", " ".join(decision.reasons))

    def test_non_groww_or_delayed_source_is_rejected(self):
        for label, kwargs in (
            ("yahoo", {"source": "yahoo"}),
            ("unknown", {"source": "unknown"}),
            ("delayed", {"delayed": True}),
        ):
            with self.subTest(label=label):
                decision = self._evaluate(make_candidate(**kwargs))
                self.assertFalse(decision.allowed)
                self.assertFalse(self._checks(decision)["data_freshness"].ok)

    def test_score_below_85_is_rejected(self):
        decision = self._evaluate(make_candidate(score=84.9))
        self.assertFalse(decision.allowed)
        self.assertFalse(self._checks(decision)["entry_quality"].ok)
        self.assertIn("84.9", " ".join(decision.reasons))

    def test_risk_reward_below_2_is_rejected(self):
        decision = self._evaluate(make_candidate(risk_reward=1.99))
        self.assertFalse(decision.allowed)
        self.assertFalse(self._checks(decision)["risk_reward"].ok)

    def test_duplicate_held_symbol_is_rejected(self):
        decision = self._evaluate(held={"TEST"})
        self.assertFalse(decision.allowed)
        self.assertFalse(self._checks(decision)["duplicate"].ok)
        self.assertIn("already held", " ".join(decision.reasons))

    def test_duplicate_ledger_position_is_rejected(self):
        with patch.object(rules_module.ledger, "positions", return_value=[
            {"symbol": "TEST", "qty": 1, "avg_price": 100.0, "last_price": 100.0}
        ]):
            decision = self._evaluate()
        self.assertFalse(decision.allowed)
        self.assertFalse(self._checks(decision)["duplicate"].ok)

    def test_daily_loss_gate_failure_is_rejected(self):
        self.can_open.return_value = (False, "daily loss limit hit (-310)")
        decision = self._evaluate()
        self.assertFalse(decision.allowed)
        self.assertFalse(self._checks(decision)["portfolio_gates"].ok)
        self.assertIn("daily loss limit hit", " ".join(decision.reasons))

    def test_outside_market_window_is_rejected(self):
        self.in_window.return_value = False
        decision = self._evaluate()
        self.assertFalse(decision.allowed)
        self.assertFalse(self._checks(decision)["market_session"].ok)

    def test_config_cannot_lower_score_or_rr_floors(self):
        self.override_cfg(("entry", "min_score"), 40)
        self.override_cfg(("entry", "minimum_score"), 40)
        self.override_cfg(("entry", "min_risk_reward"), 1.0)
        low_score = self._evaluate(make_candidate(score=84.0))
        low_rr = self._evaluate(make_candidate(risk_reward=1.5))
        self.assertEqual(low_score.computed["min_score"], 85.0)
        self.assertEqual(low_rr.computed["min_rr"], 2.0)
        self.assertFalse(low_score.allowed)
        self.assertFalse(low_rr.allowed)

    def test_rule_decision_is_persisted_to_scratch_store(self):
        decision = self._evaluate()
        self.assertEqual(Path(storage.db_path()).resolve(), Path(self.db_path).resolve())
        with storage._conn() as conn:  # scratch DB only
            count = conn.execute("SELECT COUNT(*) FROM rule_decisions").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertTrue(decision.allowed)

    def test_repeated_rule_decisions_for_same_snapshot_are_persisted(self):
        """Every evaluation gets a unique evidence row."""
        self._evaluate()
        self._evaluate()
        with storage._conn() as conn:  # scratch DB only
            count = conn.execute("SELECT COUNT(*) FROM rule_decisions").fetchone()[0]
        self.assertEqual(count, 2)


# ---------------------------------------------------------------------------
# 3. Orchestrator + engine hook
# ---------------------------------------------------------------------------
class OrchestratorQATests(ScratchDBTest):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(patch.object(ai_config, "enabled", return_value=True))
        self.enterContext(patch.object(ai_config, "fail_closed", return_value=True))
        self.enterContext(patch.object(ai_config, "require_for_entries",
                                       return_value=True))
        self.signal = {"symbol": "TEST", "price": 100.0}
        self.snapshot = make_snapshot()

    @staticmethod
    def _orchestrator(agents, debate, rules) -> Orchestrator:
        return Orchestrator(agents=agents, debate=debate, rules=rules, store=storage)

    def test_disabled_path_allows_without_running_any_ai_gate(self):
        with patch.object(ai_config, "enabled", return_value=False):
            agents = _SpyAgents(exc=RuntimeError("must not run"))
            debate = _SpyDebate(make_debate("reject_candidate"))
            rules = _SpyRules(allowed=False, reasons=["must not run"])
            decision = self._orchestrator(agents, debate, rules).evaluate(
                self.signal, snapshot=self.snapshot)
        self.assertEqual(decision.action, "ALLOW")
        self.assertEqual(decision.rationale, DISABLED_RATIONALE)
        self.assertFalse(decision.model_ok)
        self.assertEqual((agents.calls, debate.calls, rules.calls), (0, 0, 0))

    def test_agent_exception_fails_closed(self):
        agents = _SpyAgents(exc=RuntimeError("model service down"))
        debate = _SpyDebate(make_debate())
        rules = _SpyRules(allowed=True)
        decision = self._orchestrator(agents, debate, rules).evaluate(
            self.signal, snapshot=self.snapshot)
        self.assertEqual(decision.action, "REJECT")
        self.assertFalse(decision.model_ok)
        self.assertIn("no agent reports available", decision.rationale)

    def test_error_reports_fail_closed(self):
        error_reports = [make_report(a, status="error",
                                     objections=[make_objection("model_failure", "no model")])
                         for a in AGENT_IDS]
        agents = _SpyAgents(error_reports)
        debate = _SpyDebate(make_debate())
        rules = _SpyRules(allowed=True)
        decision = self._orchestrator(agents, debate, rules).evaluate(
            self.signal, snapshot=self.snapshot)
        self.assertEqual(decision.action, "REJECT")
        self.assertFalse(decision.model_ok)
        self.assertIn("status=error", decision.rationale)

    def test_debate_rejection_refuses_allow(self):
        agents = _SpyAgents(make_panel())
        debate = _SpyDebate(make_debate("reject_candidate"))
        rules = _SpyRules(allowed=True)
        decision = self._orchestrator(agents, debate, rules).evaluate(
            self.signal, snapshot=self.snapshot)
        self.assertEqual(decision.action, "REJECT")
        self.assertIn("debate=reject_candidate", decision.rationale)
        self.assertTrue(decision.model_ok)

    def test_debate_insufficient_evidence_refuses_allow(self):
        agents = _SpyAgents(make_panel()[:4])
        debate = _SpyDebate(make_debate("insufficient_evidence"))
        rules = _SpyRules(allowed=True)
        decision = self._orchestrator(agents, debate, rules).evaluate(
            self.signal, snapshot=self.snapshot)
        self.assertEqual(decision.action, "REJECT")
        self.assertIn("debate=insufficient_evidence", decision.rationale)

    def test_rule_rejection_refuses_allow(self):
        agents = _SpyAgents(make_panel())
        debate = _SpyDebate(make_debate())
        rules = _SpyRules(allowed=False, reasons=["score below floor"])
        decision = self._orchestrator(agents, debate, rules).evaluate(
            self.signal, snapshot=self.snapshot)
        self.assertEqual(decision.action, "REJECT")
        self.assertIn("score below floor", decision.rationale)

    def test_all_gates_pass_allows(self):
        agents = _SpyAgents(make_panel())
        debate = _SpyDebate(make_debate())
        rules = _SpyRules(allowed=True)
        decision = self._orchestrator(agents, debate, rules).evaluate(
            self.signal, snapshot=self.snapshot)
        self.assertEqual(decision.action, "ALLOW")
        self.assertTrue(decision.model_ok)
        self.assertEqual(decision.reject_reasons, [])

    def test_unresolved_critical_objection_refuses_allow(self):
        reports = make_panel(**{
            "risk_analyst": {"objections": [make_objection("risk", "uncapped loss")]},
        })
        agents = _SpyAgents(reports)
        debate = _SpyDebate(make_debate())
        rules = _SpyRules(allowed=True)
        decision = self._orchestrator(agents, debate, rules).evaluate(
            self.signal, snapshot=self.snapshot)
        self.assertEqual(decision.action, "REJECT")
        # With an injected debate result, the orchestrator reports the flagged
        # report id; an unresolved objection message appears when the real
        # DebateEngine supplies the objection (asserted in DebateQATests).
        self.assertIn("critical objection flagged by: risk_analyst",
                      decision.rationale)

    def test_exception_in_evaluation_fails_closed(self):
        with patch.object(orchestrator_module, "build_snapshot",
                          side_effect=RuntimeError("snapshot exploded")):
            decision = Orchestrator(store=storage).evaluate(self.signal)
        self.assertEqual(decision.action, "REJECT")
        self.assertFalse(decision.model_ok)
        self.assertIn(orchestrator_module.ERROR_REASON, decision.rationale)

    def test_exception_always_fails_closed_even_if_require_flag_is_disabled(self):
        """An evaluation error rejects a new entry regardless of the flag."""
        with patch.object(ai_config, "require_for_entries", return_value=False):
            with patch.object(orchestrator_module, "build_snapshot",
                              side_effect=RuntimeError("snapshot exploded")):
                decision = Orchestrator(store=storage).evaluate(self.signal)
        self.assertEqual(decision.action, "REJECT")
        self.assertFalse(decision.model_ok)
        self.assertIn(orchestrator_module.ERROR_REASON, decision.rationale)

    def test_engine_hook_disabled_leaves_deterministic_path_unchanged(self):
        from engine import engine as engine_module

        def disabled_cfg(*keys, default=None):
            if keys == ("intelligence", "enabled"):
                return False
            return settings.cfg(*keys, default=default)

        spy = _SpyAgents(exc=RuntimeError("must not run"))
        with patch.object(engine_module, "cfg", side_effect=disabled_cfg), \
             patch.object(orchestrator_module, "ORCHESTRATOR",
                          Orchestrator(agents=spy, store=storage)):
            allowed, payload = engine_module._intelligence_gate(self.signal, set())
        self.assertTrue(allowed)
        self.assertIsNone(payload)
        self.assertEqual(spy.calls, 0)

    def test_engine_hook_blocks_on_reject_and_on_exception(self):
        from engine import engine as engine_module

        enabled_orchestrator = Orchestrator(
            agents=_SpyAgents(make_panel()),
            debate=_SpyDebate(make_debate("reject_candidate")),
            rules=_SpyRules(allowed=True), store=storage)
        with patch.object(engine_module, "cfg", return_value=True), \
             patch.object(orchestrator_module, "ORCHESTRATOR", enabled_orchestrator):
            allowed, payload = engine_module._intelligence_gate(self.signal, set())
        self.assertFalse(allowed)
        self.assertEqual(payload["action"], "REJECT")
        self.assertIn("debate=reject_candidate", payload["rationale"])

        class _Broken:
            def evaluate(self, *args, **kwargs):
                raise RuntimeError("orchestrator unavailable")

        with patch.object(engine_module, "cfg", return_value=True), \
             patch.object(orchestrator_module, "ORCHESTRATOR", _Broken()):
            allowed, payload = engine_module._intelligence_gate(self.signal, set())
        self.assertFalse(allowed)
        self.assertEqual(payload["action"], "REJECT")
        self.assertIn("intelligence gate unavailable", payload["rationale"])
        error_logs = [row for row in storage.executions()
                      if row["event"] == "intelligence_gate_error"]
        self.assertTrue(error_logs)
        self.assertTrue(all(row["paper_only"] == 1 for row in error_logs))


# ---------------------------------------------------------------------------
# 4. Model adapter
# ---------------------------------------------------------------------------
class ModelAdapterQATests(unittest.TestCase):
    SECRET = "QA-SUPER-SECRET-998877"
    OTHER_SECRET = "QA-OTHER-SECRET-445566"

    def _adapter(self, store: _UsageStore, **overrides: Any):
        cfg = {
            "base_url": "https://api.groq.com/openai/v1", "model": "qa-model",
            "api_key_env": "", "retries": 0, "rate_limit_per_minute": 0,
            "structured": True, "temperature": 0.1, "max_tokens": 100,
        }
        cfg.update(overrides)
        return adapter_module.ModelAdapter(config=cfg, store=store)

    def _response_bytes(self, content: str,
                        usage: dict | None = None) -> bytes:
        return json.dumps({
            "choices": [{"message": {"content": content}}],
            "usage": usage or {"prompt_tokens": 12, "completion_tokens": 3,
                               "total_tokens": 15},
        }).encode("utf-8")

    def test_no_environment_dump_in_request_body_or_usage(self):
        store = _UsageStore()
        adapter = self._adapter(store, api_key_env="GROQ_API_KEY")
        capture = _UrlopenCapture(self._response_bytes(
            json.dumps({"verdict": "ok", "score": 91})))
        env = _SpyEnviron({"GROQ_API_KEY": self.SECRET,
                           "QA_OTHER_SECRET": self.OTHER_SECRET})
        with patch.object(adapter_module.os, "environ", env), \
             patch.object(adapter_module.urllib.request, "urlopen", capture):
            result = adapter.complete(_QAOutput, "system instructions",
                                      {"candidate": "TEST"}, "qa_agent",
                                      correlation_id="qa-corr-1")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["score"], 91)
        self.assertEqual(env.get_calls, ["GROQ_API_KEY"])
        self.assertEqual(len(capture.calls), 1)
        request = capture.calls[0][0]
        self.assertEqual(request.full_url, "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(request.headers.get("Authorization"), f"Bearer {self.SECRET}")
        self.assertTrue(any(k.lower() == "user-agent" for k in request.headers))
        body_text = request.data.decode("utf-8")
        self.assertNotIn(self.SECRET, body_text)
        self.assertNotIn(self.OTHER_SECRET, body_text)
        for token in ("api_key", "GROQ_API_KEY", "environ", "getenv", "QA_OTHER_SECRET"):
            self.assertNotIn(token.lower(), body_text.lower())
        self.assertEqual(len(store.usages), 1)
        usage_blob = store.usages[0].model_dump_json()
        self.assertNotIn(self.SECRET, usage_blob)
        self.assertNotIn(self.OTHER_SECRET, usage_blob)
        self.assertTrue(store.usages[0].ok)
        self.assertEqual(store.usages[0].total_tokens, 15)
        self.assertEqual(store.usages[0].correlation_id, "qa-corr-1")

    def test_missing_api_key_fails_cleanly_and_logs_usage(self):
        store = _UsageStore()
        adapter = self._adapter(store, api_key_env="GROQ_API_KEY")
        env = _SpyEnviron({})
        with patch.object(adapter_module.os, "environ", env), \
             patch.object(adapter_module.urllib.request, "urlopen",
                          _UrlopenCapture()) as capture:
            result = adapter.complete(_QAOutput, "sys", {"x": 1}, "qa_agent")
            health = adapter.health(record=False)
        self.assertFalse(result.ok)
        self.assertIsNone(result.data)
        self.assertIn("not set", result.error or "")
        self.assertFalse(health.ok)
        self.assertFalse(health.paid_allowed)
        self.assertEqual(capture.calls, [])
        self.assertEqual(len(store.usages), 1)  # complete only; polling health is not usage
        self.assertTrue(all(not usage.ok for usage in store.usages))

    def test_health_recording_can_be_disabled_for_polling(self):
        store = _UsageStore()
        adapter = self._adapter(store, api_key_env="GROQ_API_KEY")
        with patch.object(adapter_module.os, "environ", _SpyEnviron({})):
            health = adapter.health(record=False)
            adapter.health()
        self.assertFalse(health.ok)
        self.assertEqual(len(store.usages), 1)

    def test_health_probe_is_structured_and_identifies_client(self):
        store = _UsageStore()
        adapter = self._adapter(store, api_key_env="GROQ_API_KEY")
        capture = _UrlopenCapture(self._response_bytes('{"ok": true}'))
        env = _SpyEnviron({"GROQ_API_KEY": self.SECRET})
        with patch.object(adapter_module.os, "environ", env), \
             patch.object(adapter_module.urllib.request, "urlopen", capture):
            health = adapter.health(record=False)
        self.assertTrue(health.ok, health.message)
        request = capture.calls[0][0]
        self.assertTrue(any(k.lower() == "user-agent" for k in request.headers))
        body = json.loads(request.data.decode("utf-8"))
        self.assertIn("json", json.dumps(body).lower())
        self.assertEqual(body.get("response_format"), {"type": "json_object"})

    def test_health_endpoint_disables_usage_recording(self):
        calls = []

        class _Probe:
            def health(self, **kwargs):
                calls.append(kwargs)
                return {"ok": True, "healthy": True}

        with patch.object(adapter_module, "ModelAdapter", _Probe):
            result = intelligence_api.intelligence_health()
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [{"record": False}])

    def test_structured_json_is_validated(self):
        store = _UsageStore()
        adapter = self._adapter(store, api_key_env="GROQ_API_KEY")
        env = _SpyEnviron({"GROQ_API_KEY": self.SECRET})
        valid = json.dumps({"verdict": "ok", "score": 91})
        cases = (
            ("plain_json", valid, True),
            ("fenced_json", f"```json\n{valid}\n```", True),
            ("missing_field", json.dumps({"verdict": "ok"}), False),
            ("not_an_object", "[1, 2, 3]", False),
        )
        for label, content, expected_ok in cases:
            with self.subTest(label=label):
                store.usages.clear()
                capture = _UrlopenCapture(self._response_bytes(content))
                with patch.object(adapter_module.os, "environ", env), \
                     patch.object(adapter_module.urllib.request, "urlopen", capture), \
                     patch.object(adapter_module.time, "sleep"):
                    result = adapter.complete(_QAOutput, "sys", {"x": 1}, "qa_agent")
                self.assertEqual(result.ok, expected_ok, result.error)
                self.assertEqual(len(store.usages), 1)
                self.assertEqual(store.usages[0].ok, expected_ok)
                if not expected_ok:
                    self.assertTrue(result.error)
                    self.assertNotIn(self.SECRET, result.error)

    def test_secret_is_redacted_from_http_error_usage(self):
        store = _UsageStore()
        adapter = self._adapter(store, api_key_env="GROQ_API_KEY")
        env = _SpyEnviron({"GROQ_API_KEY": self.SECRET})
        http_error = urllib.error.HTTPError(
            "https://api.groq.com/openai/v1/chat/completions", 401,
            f"Unauthorized {self.SECRET}", None, None)
        capture = _UrlopenCapture(exc=http_error)
        with patch.object(adapter_module.os, "environ", env), \
             patch.object(adapter_module.urllib.request, "urlopen", capture):
            result = adapter.complete(_QAOutput, "sys", {"x": 1}, "qa_agent")
        self.assertFalse(result.ok)
        self.assertNotIn(self.SECRET, result.error or "")
        self.assertNotIn(self.SECRET, store.usages[0].model_dump_json())
        self.assertIn("HTTP 401", result.error or "")

    def test_local_endpoint_never_receives_hosted_credential(self):
        store = _UsageStore()
        adapter = self._adapter(
            store, api_key_env="GROQ_API_KEY",
            local_endpoint="http://127.0.0.1:9999/v1", local_model="qa-local")
        capture = _UrlopenCapture(self._response_bytes(
            json.dumps({"verdict": "local", "score": 70})))
        env = _SpyEnviron({"GROQ_API_KEY": self.SECRET})
        with patch.object(adapter_module.os, "environ", env), \
             patch.object(adapter_module.urllib.request, "urlopen", capture):
            result = adapter.complete(_QAOutput, "sys", {"x": 1}, "qa_agent")
        self.assertTrue(result.ok, result.error)
        request = capture.calls[0][0]
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn(self.SECRET, request.data.decode("utf-8"))


# ---------------------------------------------------------------------------
# 5. Evolution
# ---------------------------------------------------------------------------
BASE_TEXT = settings.CONFIG_PATH.read_text(encoding="utf-8")
BASE_DATA = yaml.safe_load(BASE_TEXT)


class EvolutionQATests(ScratchDBTest):
    ABSOLUTE_UNSAFE = (
        ("app.mode=live", ("app", "mode"), "live"),
        ("engine.execution_mode=live", ("engine", "execution_mode"), "live"),
        ("entry.minimum_score=84", ("entry", "minimum_score"), 84),
        ("entry.min_risk_reward=1.9", ("entry", "min_risk_reward"), 1.9),
        ("entry.risk.minimum_risk_reward=1.5",
         ("entry", "risk", "minimum_risk_reward"), 1.5),
        ("risk.minimum_risk_reward=1.5", ("risk", "minimum_risk_reward"), 1.5),
        ("exit.target.minimum_risk_reward=1.5",
         ("exit", "target", "minimum_risk_reward"), 1.5),
        ("allow_live_orders=true",
         ("execution", "live_trading", "allow_live_orders"), True),
        ("safety.live_trading_disabled=false",
         ("safety", "live_trading_disabled"), False),
        ("safety.real_order_placement_disabled=false",
         ("safety", "real_order_placement_disabled"), False),
        ("slippage=0", ("costs", "slippage_pct"), 0),
        ("simulate_all_charges=false", ("costs", "simulate_all_charges"), False),
        ("backtesting.include_slippage=false",
         ("backtesting", "include_slippage"), False),
    )
    DIFF_UNSAFE = (
        ("app.mode", ("app", "mode"), "live"),
        ("account.initial_capital", ("account", "initial_capital"), 999999),
        ("account.intraday_leverage", ("account", "intraday_leverage"), 10),
        ("account.risk_per_trade", ("account", "risk_per_trade"), 5000),
        ("account.max_daily_loss", ("account", "max_daily_loss"), 1),
        ("account.max_positions", ("account", "max_positions"), 20),
        ("engine.enabled", ("engine", "enabled"), False),
        ("safety.paper_trading_only", ("safety", "paper_trading_only"), False),
        ("entry.minimum_score", ("entry", "minimum_score"), 80),
        ("entry.min_risk_reward", ("entry", "min_risk_reward"), 1.5),
        ("costs.slippage_pct", ("costs", "slippage_pct"), 0.0),
        ("engine.entry_start_time", ("engine", "entry_start_time"), "00:00"),
    )

    def test_absolute_safety_guard_rejects_unsafe_documents(self):
        evolution_module._check_absolute_safety(copy.deepcopy(BASE_DATA))
        for label, path, value in self.ABSOLUTE_UNSAFE:
            with self.subTest(label=label):
                doc = copy.deepcopy(BASE_DATA)
                set_path(doc, path, value)
                with self.assertRaises(ValueError):
                    evolution_module._check_absolute_safety(doc)

    def test_only_safe_paths_diff_is_allowed(self):
        for label, path, value in self.DIFF_UNSAFE:
            with self.subTest(label=label):
                changed = copy.deepcopy(BASE_DATA)
                set_path(changed, path, value)
                with self.assertRaises(ValueError):
                    evolution_module._assert_only_safe_changes(BASE_DATA, changed)

        safe = copy.deepcopy(BASE_DATA)
        set_path(safe, ("exit", "target", "target_r_mult"), 2.5)
        evolution_module._assert_only_safe_changes(BASE_DATA, safe)

    def test_generated_candidates_only_change_safe_leaves(self):
        engine = evolution_module.StrategyEvolutionEngine(store=_MemoryStore())
        candidates = engine._generate_candidates(BASE_TEXT, BASE_DATA, 24)
        self.assertTrue(candidates)
        allowed = {dotted(path) for path in evolution_module.SAFE_PATHS}
        for candidate in candidates:
            with self.subTest(path=candidate["path"]):
                data = yaml.safe_load(candidate["text"])
                evolution_module._assert_only_safe_changes(BASE_DATA, data)
                self.assertTrue(set(candidate["changed"]).issubset(allowed))
                self.assertEqual(str(data["app"]["mode"]).lower(), "paper")
                self.assertEqual(str(data["engine"]["execution_mode"]).lower(), "paper")
                self.assertEqual(float(data["entry"]["minimum_score"]), 85.0)
                for path in (("entry", "min_risk_reward"),
                             ("entry", "risk", "minimum_risk_reward"),
                             ("risk", "minimum_risk_reward"),
                             ("exit", "target", "minimum_risk_reward")):
                    value = evolution_module._get(data, path)
                    if value is not None:
                        self.assertGreaterEqual(float(value), 2.0)

    def test_rollback_restores_previous_version_without_writing_config(self):
        store = _MemoryStore()
        v1 = StrategyVersion(
            ts="2026-11-21T10:00:00+05:30", status="rolled_back", yaml=BASE_TEXT,
            params={"source": "baseline"}, metrics={"validation_passed": True},
            rationale="original baseline")
        mutated_text = evolution_module._replace_yaml_scalar(
            BASE_TEXT, ("exit", "target", "target_r_mult"), 2.5)
        v2 = StrategyVersion(
            ts="2026-11-22T10:00:00+05:30", status="active", yaml=mutated_text,
            params={"source": "candidate"}, metrics={"validation_passed": True},
            parent_version_id=v1.version_id, rationale="safe candidate")
        store.save_version(v1)
        store.save_version(v2)

        before = hashlib.sha256(settings.CONFIG_PATH.read_bytes()).hexdigest()
        written: list[str] = []

        def no_disk_write(text: str) -> None:
            written.append(text)

        with patch.object(evolution_module, "_atomic_write_strategy",
                          side_effect=no_disk_write):
            result = evolution_module.StrategyEvolutionEngine(store=store).rollback()
        after = hashlib.sha256(settings.CONFIG_PATH.read_bytes()).hexdigest()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(result["rolled_back_from"], v2.version_id)
        self.assertEqual(written, [v1.yaml])
        self.assertEqual(store.get_version(v1.version_id)["status"], "active")
        self.assertEqual(store.get_version(v2.version_id)["status"], "rolled_back")
        self.assertEqual(store.active_version()["id"], v1.version_id)
        self.assertEqual(before, after, "config/strategy.yaml was modified")

    def test_promotion_rejects_unsafe_yaml_without_writing(self):
        store = _MemoryStore()
        live_text = BASE_TEXT.replace('mode: "paper"', 'mode: "live"', 1)
        self.assertNotEqual(live_text, BASE_TEXT)
        bad = StrategyVersion(
            ts=QA_TS, status="validated", yaml=live_text,
            metrics={"validation_passed": True}, rationale="unsafe")
        store.save_version(bad)
        with patch.object(evolution_module, "_atomic_write_strategy") as write:
            with self.assertRaises(ValueError):
                evolution_module.StrategyEvolutionEngine(store=store).promote(
                    bad.version_id, require_shadow=False)
            write.assert_not_called()

    def test_promotion_requires_real_shadow_evidence(self):
        store = _MemoryStore()
        version = StrategyVersion(
            ts=QA_TS, status="shadow", yaml=BASE_TEXT,
            metrics={"validation_passed": True, "shadow_sessions_completed": 0,
                     "shadow_sessions_required": 5},
            rationale="not yet shadow-tested")
        store.save_version(version)
        with patch.object(evolution_module, "_atomic_write_strategy") as write:
            with self.assertRaises(ValueError) as ctx:
                evolution_module.StrategyEvolutionEngine(store=store).promote(
                    version.version_id, require_shadow=True)
            self.assertIn("shadow evidence", str(ctx.exception))
            write.assert_not_called()

    def test_promotion_rejects_tampered_capital_or_leverage(self):
        """Promotion re-checks that only safe YAML leaves changed."""
        store = _MemoryStore()
        baseline = StrategyVersion(
            ts="2026-11-21T10:00:00+05:30", status="active", yaml=BASE_TEXT,
            metrics={"validation_passed": True})
        store.save_version(baseline)
        tampered_text = evolution_module._replace_yaml_scalar(
            BASE_TEXT, ("account", "initial_capital"), 999999)
        tampered_text = evolution_module._replace_yaml_scalar(
            tampered_text, ("account", "intraday_leverage"), 10)
        tampered = StrategyVersion(
            ts=QA_TS, status="validated", yaml=tampered_text,
            parent_version_id=baseline.version_id,
            metrics={"validation_passed": True}, rationale="tampered version")
        store.save_version(tampered)

        written: list[str] = []

        def validator_still_runs(text: str) -> None:
            # Preserve the real main-app validator while avoiding any disk write.
            evolution_module._validate_yaml_for_write(text)
            written.append(text)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            with patch.object(evolution_module, "_atomic_write_strategy",
                              side_effect=validator_still_runs):
                with self.assertRaises(ValueError):
                    evolution_module.StrategyEvolutionEngine(store=store).promote(
                        tampered.version_id, require_shadow=False)
        self.assertEqual(written, [])


# ---------------------------------------------------------------------------
# 6. Security remediation evidence
# ---------------------------------------------------------------------------
class _TokenRequest:
    """Minimal duck-typed stand-in for FastAPI's Request."""

    def __init__(self, token: str = "") -> None:
        self.headers = {"x-intelligence-token": token} if token else {}


class SecurityRemediationQATests(ScratchDBTest):
    def test_admin_token_is_required_and_constant_time_checked(self):
        with patch.dict(os.environ, {"INTELLIGENCE_ADMIN_TOKEN": "qa-admin-secret"}):
            intelligence_api._require_admin(_TokenRequest("qa-admin-secret"))
            with self.assertRaises(HTTPException) as wrong:
                intelligence_api._require_admin(_TokenRequest("not-the-token"))
            self.assertEqual(wrong.exception.status_code, 401)
        with patch.dict(os.environ, {"INTELLIGENCE_ADMIN_TOKEN": ""}):
            with self.assertRaises(HTTPException) as disabled:
                intelligence_api._require_admin(_TokenRequest("anything"))
            self.assertEqual(disabled.exception.status_code, 503)

    def test_evolution_route_enforces_admin_gate(self):
        with patch.dict(os.environ, {"INTELLIGENCE_ADMIN_TOKEN": "qa-admin-secret"}):
            with self.assertRaises(HTTPException) as denied:
                intelligence_api.intelligence_evolution_run(_TokenRequest())
            self.assertEqual(denied.exception.status_code, 401)

    def test_model_adapter_blocks_unapproved_env_and_endpoint(self):
        adapter = adapter_module.ModelAdapter(config={
            "base_url": "https://evil.example/v1", "model": "evil-model",
            "api_key_env": "AWS_SECRET_ACCESS_KEY", "retries": 0,
            "rate_limit_per_minute": 0, "structured": True,
            "temperature": 0.1, "max_tokens": 100,
        }, store=_UsageStore())
        response = json.dumps({
            "choices": [{"message": {"content": '{"verdict":"ok","score":90}'}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode("utf-8")
        capture = _UrlopenCapture(response)
        env = _SpyEnviron({"AWS_SECRET_ACCESS_KEY": "MUST-NOT-LEAK"})
        with patch.object(adapter_module.os, "environ", env), \
             patch.object(adapter_module.urllib.request, "urlopen", capture):
            result = adapter.complete(_QAOutput, "sys", {"x": 1}, "qa_agent")
        self.assertFalse(result.ok)
        self.assertIn("api_key_env", result.error or "")
        self.assertEqual(env.get_calls, [])
        self.assertEqual(capture.calls, [])

    def test_strategy_editor_rejects_credential_redirection(self):
        data = yaml.safe_load(BASE_TEXT)
        data["intelligence"]["model"]["api_key_env"] = "AWS_SECRET_ACCESS_KEY"
        data["intelligence"]["model"]["base_url"] = "https://evil.example/v1"
        with self.assertRaises(HTTPException) as denied:
            engine_api._validate_strategy(yaml.safe_dump(data))
        self.assertEqual(denied.exception.status_code, 400)

    def test_ensure_baseline_seeds_one_rollback_target(self):
        store = _MemoryStore()
        engine = evolution_module.StrategyEvolutionEngine(store=store)
        with patch.object(evolution_module, "load_active_yaml", return_value=BASE_TEXT):
            first = engine.ensure_baseline()
            second = engine.ensure_baseline()
        self.assertIsNotNone(first)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(store.active_version()["id"], first["id"])

    def test_rollback_falls_back_to_backup_when_store_has_no_target(self):
        store = _MemoryStore()
        active = StrategyVersion(
            ts="2026-11-22T10:00:00+05:30", status="active", yaml=BASE_TEXT,
            metrics={"validation_passed": True})
        store.save_version(active)
        engine = evolution_module.StrategyEvolutionEngine(store=store)
        with patch.object(engine, "restore_from_backup",
                          return_value={"ok": True, "status": "restored_from_backup"}) as restore:
            result = engine.rollback()
        restore.assert_called_once_with()
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(result["rolled_back_from"], active.version_id)

    def test_restore_from_backup_validates_and_preserves_backup(self):
        mutated = evolution_module._replace_yaml_scalar(
            BASE_TEXT, ("exit", "target", "target_r_mult"), 2.5)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "strategy.yaml"
            backup = target.with_name(target.name + ".bak")
            target.write_text(BASE_TEXT, encoding="utf-8")
            backup.write_text(mutated, encoding="utf-8")
            with patch.object(evolution_module.settings, "CONFIG_PATH", target), \
                 patch.object(evolution_module.settings, "load_config"):
                result = evolution_module.StrategyEvolutionEngine(
                    store=_MemoryStore()).restore_from_backup()
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["status"], "restored_from_backup")
            self.assertEqual(target.read_text(encoding="utf-8"), mutated)
            self.assertEqual(backup.read_text(encoding="utf-8"), mutated)


# ---------------------------------------------------------------------------
# 7. Paper-only guarantees
# ---------------------------------------------------------------------------
class PaperOnlyQATests(ScratchDBTest):
    INTELLIGENCE_DIR = REPO / "engine" / "intelligence"

    def test_no_order_endpoint_in_intelligence_router(self):
        from engine.intelligence.api import router

        post_paths = {
            route.path for route in router.routes if "POST" in getattr(route, "methods", ())}
        self.assertEqual(post_paths, {
            "/api/intelligence/evolution/run",
            "/api/intelligence/strategy/{version_id}/promote",
            "/api/intelligence/strategy/rollback",
        })
        for route in router.routes:
            path = route.path.lower()
            for forbidden in ("order", "buy", "sell", "trade"):
                self.assertNotIn(forbidden, path)

    def test_router_is_mounted_in_main_app(self):
        from engine import api as main_api
        from engine.intelligence.api import router

        included = [route for route in main_api.app.routes
                    if getattr(route, "original_router", None) is router]
        self.assertEqual(len(included), 1)

    def test_no_broker_order_calls_anywhere_in_intelligence(self):
        forbidden_calls = {"buy", "sell", "place_order", "place_buy",
                           "place_sell", "create_order"}
        violations: list[str] = []
        for source in sorted(self.INTELLIGENCE_DIR.glob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = (func.attr if isinstance(func, ast.Attribute)
                            else func.id if isinstance(func, ast.Name) else "")
                    if name in forbidden_calls:
                        violations.append(f"{source.name}:{node.lineno}:{name}")
                if isinstance(node, ast.Attribute) and node.attr in forbidden_calls:
                    violations.append(f"{source.name}:{node.lineno}:attr.{node.attr}")
        self.assertEqual(violations, [], "order-like calls found")

    def test_execution_log_constructors_default_to_paper_only(self):
        violations: list[str] = []
        for source in sorted(self.INTELLIGENCE_DIR.glob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (func.attr if isinstance(func, ast.Attribute)
                        else func.id if isinstance(func, ast.Name) else "")
                if name != "ExecutionLog":
                    continue
                for keyword in node.keywords:
                    if keyword.arg == "paper_only":
                        value = keyword.value
                        if not (isinstance(value, ast.Constant) and value.value is True):
                            violations.append(f"{source.name}:{node.lineno}")
        self.assertEqual(violations, [], "non-paper execution log found")

    def test_runtime_execution_logs_are_paper_only(self):
        with patch.object(ai_config, "enabled", return_value=False):
            Orchestrator(store=storage).evaluate({"symbol": "TEST"})
        rows = storage.executions()
        self.assertTrue(rows)
        self.assertTrue(all(row["paper_only"] == 1 for row in rows))
        self.assertTrue(all(row["event"] == "intelligence_decision" for row in rows))

    def test_config_hard_forces_paper_only_and_no_paid_model(self):
        self.override_cfg(("intelligence", "paper_only"), False)
        self.override_cfg(("intelligence", "model"), {"allow_paid": True})
        cfg = ai_config.config()
        self.assertTrue(cfg["paper_only"])
        self.assertFalse(cfg["model"]["allow_paid"])


# ---------------------------------------------------------------------------
# 7. Frontend build artefacts (existence only)
# ---------------------------------------------------------------------------
class FrontendBuildQATests(unittest.TestCase):
    DIST = REPO / "frontend" / "dist"
    SRC = REPO / "frontend" / "src"

    def test_dist_references_existing_nonempty_assets(self):
        index = self.DIST / "index.html"
        self.assertTrue(index.is_file(), "frontend/dist/index.html missing")
        html = index.read_text(encoding="utf-8")
        refs = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)
        self.assertTrue(refs, "no /assets/... references in dist/index.html")
        suffixes = set()
        for ref in refs:
            asset = self.DIST / ref.lstrip("/")
            self.assertTrue(asset.is_file(), f"missing built asset: {asset}")
            self.assertGreater(asset.stat().st_size, 0, f"empty asset: {asset}")
            suffixes.add(asset.suffix.lower())
        self.assertIn(".js", suffixes)
        self.assertIn(".css", suffixes)

    def test_src_build_inputs_exist(self):
        required = (
            self.SRC / "main.tsx", self.SRC / "App.tsx", self.SRC / "api.ts",
            self.SRC / "types.ts", self.SRC / "index.css",
            self.SRC / "components" / "IntelligencePanel.tsx",
            REPO / "frontend" / "package.json", REPO / "frontend" / "vite.config.ts",
        )
        for path in required:
            with self.subTest(path=str(path)):
                self.assertTrue(path.is_file(), f"missing source/build file: {path}")
                self.assertGreater(path.stat().st_size, 0, f"empty file: {path}")


class ModeSwitchQATests(unittest.TestCase):
    def test_mode_switch_changes_only_enabled_flag(self):
        text = (REPO / "config" / "strategy.yaml").read_text(encoding="utf-8")
        normal = engine_api._set_scalar(text, "intelligence", "enabled", False)
        ai = engine_api._set_scalar(text, "intelligence", "enabled", True)
        engine_api._validate_strategy(normal)
        engine_api._validate_strategy(ai)
        normal_data = yaml.safe_load(normal)
        ai_data = yaml.safe_load(ai)
        self.assertIs(normal_data["intelligence"]["enabled"], False)
        self.assertIs(ai_data["intelligence"]["enabled"], True)
        normal_data["intelligence"]["enabled"] = ai_data["intelligence"]["enabled"]
        self.assertEqual(normal_data, ai_data)

    def test_mode_flag_must_be_boolean(self):
        with self.assertRaises(HTTPException):
            engine_api._validate_intelligence_config({"intelligence": {"enabled": "yes"}})


if __name__ == "__main__":
    unittest.main(verbosity=2)
