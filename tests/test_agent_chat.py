"""QA for the live, read-only agent group chat.

The tests use fake agents/adapters/pipelines, so they never call a model and
never need network access. They cover event streaming, fail-closed wiring,
session limits and the SSE frame format.
"""
from __future__ import annotations

import json
import threading
import time
import unittest
from typing import Any
from unittest.mock import patch

from engine.intelligence import chat
from engine.intelligence import config as ai_config
from engine.intelligence.agents import RuntimeAgents
from engine.intelligence.debate import DebateEngine
from engine.intelligence.models import (
    AgentReport,
    Evidence,
    RuleCheck,
    RuleDecision,
    TradeDecision,
)
from engine.intelligence.orchestrator import Orchestrator
from engine.intelligence.prompts import AGENT_IDS, AGENT_NAMES
from engine.intelligence.snapshot import build_snapshot

SCAN = {
    "ts": "2026-11-23T10:00:00+05:30",
    "source": "yahoo",
    "scanned": 1,
    "qualified": 1,
    "results": [
        {
            "symbol": "UNITDSPR",
            "price": 1485.0,
            "score": 84.2,
            "status": "QUALIFIED",
            "confirmations": 5,
            "source": "yahoo",
            "delayed": True,
            "quote_age_sec": 5,
            "data_stale": False,
            "as_of": "2026-11-23T09:59:55+05:30",
            "indicators": {"rsi": 62.0, "atr_pct": 1.2},
            "reasons": ["qa fixture"],
        }
    ],
}


class FakeStore:
    def __init__(self) -> None:
        self.decision = None
        self.debate = None

    def init_db(self) -> None:
        pass

    def save_trade_decision(self, decision) -> None:
        self.decision = decision

    def save_execution_log(self, log) -> None:
        pass

    def save_debate(self, result) -> None:
        self.debate = result

    def save_report(self, report) -> None:
        pass


def _report(snapshot, agent_id: str, symbol: str, **overrides: Any) -> AgentReport:
    data = {
        "agent_id": agent_id,
        "agent_name": AGENT_NAMES.get(agent_id, agent_id),
        "symbol": symbol,
        "snapshot_id": snapshot.snapshot_id,
        "ts": "2026-11-23T10:00:00+05:30",
        "status": "ok",
        "summary": f"{agent_id} reviewed the setup.",
        "evidence": [Evidence(source="candidate", key="price", value=1485.0,
                              ref="candidate.price")],
        "uncertainty": 0.2,
        "confidence": 0.8,
        "recommendation": "support",
        "model": "qa-model",
    }
    data.update(overrides)
    return AgentReport(**data)


class EventedRunner:
    """Minimal stand-in for RuntimeAgents that supports the event hook."""

    def __init__(self) -> None:
        self.seen: list[dict] = []

    def run(self, snapshot, candidates=None, on_event=None):
        symbol = candidates[0].symbol if candidates else None
        reports = []
        for agent_id in AGENT_IDS:
            if on_event:
                on_event({"type": "agent_start", "agent_id": agent_id,
                          "agent_name": AGENT_NAMES[agent_id], "symbol": symbol})
            report = _report(snapshot, agent_id, symbol)
            reports.append(report)
            if on_event:
                on_event({"type": "agent_report", "agent_id": agent_id,
                          "agent_name": AGENT_NAMES[agent_id], "symbol": symbol,
                          "status": "ok", "summary": report.summary})
        return reports


class LegacyRunner:
    """A pre-existing injected runner with no event hook must keep working."""

    def run(self, snapshot, candidates=None):
        symbol = candidates[0].symbol if candidates else None
        return [_report(snapshot, agent_id, symbol) for agent_id in AGENT_IDS]


class FakeRules:
    def __init__(self) -> None:
        self.seen = False

    def evaluate(self, symbol, snapshot, held):
        self.seen = True
        return RuleDecision(
            symbol=symbol,
            snapshot_id=snapshot.snapshot_id,
            ts="2026-11-23T10:00:01+05:30",
            allowed=True,
            checks=[RuleCheck(name="risk", ok=True, detail="within limits")],
        )


class FakeAdapter:
    def complete(self, schema, system, payload, agent_id, correlation_id=None):
        class Usage:
            model = "qa-model"
            usage_id = "usage-qa"

        class Result:
            ok = True
            error = None
            usage = Usage()
            data = {
                "agent_id": agent_id,
                "agent_name": AGENT_NAMES.get(agent_id, agent_id),
                "symbol": None,
                "snapshot_id": "runtime-owned",
                "ts": "runtime-owned",
                "status": "ok",
                "summary": f"{agent_id} says the evidence is thin.",
                "evidence": [],
                "uncertainty": 0.5,
                "confidence": 0.5,
                "recommendation": "abstain",
            }

        return Result()


def _collect(service: chat.AgentChatService, session_id: str,
             timeout: float = 5.0) -> list[dict]:
    session = service.get(session_id)
    events: list[dict] = []
    index = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        batch, done = session.read(index, timeout=0.2)
        for seq, event in batch:
            events.append(event)
            index = seq
        if done:
            return events
    raise AssertionError("chat session did not finish in time")


class OrchestratorEventTests(unittest.TestCase):
    def test_events_cover_agents_debate_and_rules(self):
        snapshot = build_snapshot(scan_result=SCAN)
        self.assertTrue(snapshot.candidates)
        events: list[dict] = []
        store = FakeStore()
        orchestrator = Orchestrator(
            agents=EventedRunner(),
            rules=FakeRules(),
            debate=DebateEngine(store=store),
            store=store,
        )
        with patch.object(ai_config, "enabled", return_value=True):
            decision = orchestrator.evaluate(
                {"symbol": "UNITDSPR"}, snapshot=snapshot, held=set(),
                on_event=events.append,
            )
        types = [event["type"] for event in events]
        self.assertEqual(types.count("agent_start"), len(AGENT_IDS))
        self.assertEqual(types.count("agent_report"), len(AGENT_IDS))
        self.assertIn("debate", types)
        self.assertIn("rules", types)
        self.assertLess(types.index("debate"), types.index("rules"))
        self.assertEqual(decision.action, "ALLOW")
        self.assertTrue(decision.model_ok)
        self.assertEqual(store.decision.decision_id, decision.decision_id)

    def test_legacy_runner_without_hook_still_works(self):
        snapshot = build_snapshot(scan_result=SCAN)
        store = FakeStore()
        orchestrator = Orchestrator(
            agents=LegacyRunner(),
            rules=FakeRules(),
            debate=DebateEngine(store=store),
            store=store,
        )
        with patch.object(ai_config, "enabled", return_value=True):
            decision = orchestrator.evaluate({"symbol": "UNITDSPR"}, snapshot=snapshot)
        self.assertEqual(decision.action, "ALLOW")

    def test_broken_listener_cannot_break_a_decision(self):
        snapshot = build_snapshot(scan_result=SCAN)
        store = FakeStore()
        orchestrator = Orchestrator(
            agents=LegacyRunner(),
            rules=FakeRules(),
            debate=DebateEngine(store=store),
            store=store,
        )

        def explode(_event):
            raise RuntimeError("listener down")

        with patch.object(ai_config, "enabled", return_value=True):
            decision = orchestrator.evaluate(
                {"symbol": "UNITDSPR"}, snapshot=snapshot, on_event=explode,
            )
        self.assertEqual(decision.action, "ALLOW")


class RuntimeAgentsEventTests(unittest.TestCase):
    def test_runtime_agents_emit_start_and_report_events(self):
        snapshot = build_snapshot(scan_result=SCAN)
        events: list[dict] = []
        runner = RuntimeAgents(adapter=FakeAdapter(), store=FakeStore())
        reports = runner.run(snapshot, [snapshot.candidates[0]],
                             on_event=events.append)
        self.assertEqual(len(reports), len(AGENT_IDS))
        starts = [event for event in events if event["type"] == "agent_start"]
        rows = [event for event in events if event["type"] == "agent_report"]
        self.assertEqual(len(starts), len(AGENT_IDS))
        self.assertEqual(len(rows), len(AGENT_IDS))
        self.assertTrue(all(row.get("summary") for row in rows))
        self.assertEqual([row["agent_id"] for row in rows], list(AGENT_IDS))


class ChatPipelineTests(unittest.TestCase):
    def test_pipeline_streams_scan_snapshot_agents_and_decision(self):
        events: list[dict] = []

        class FakeOrchestrator:
            def evaluate(self, signal, snapshot=None, held=None, on_event=None):
                on_event({"type": "agent_start", "agent_id": "market_regime",
                          "agent_name": "Market Regime", "symbol": "UNITDSPR"})
                on_event({"type": "agent_report", "agent_id": "market_regime",
                          "agent_name": "Market Regime", "symbol": "UNITDSPR",
                          "status": "error", "summary": "rate limited",
                          "recommendation": "abstain"})
                return TradeDecision(
                    symbol="UNITDSPR", action="REJECT", ts="2026-11-23T10:00:02+05:30",
                    snapshot_id=snapshot.snapshot_id, rationale="qa reject",
                    model_ok=False, reject_reasons=["market_regime: status=error"],
                )

        with patch("engine.scanner.scan", return_value=SCAN), \
             patch("engine.intelligence.orchestrator.Orchestrator", FakeOrchestrator):
            decision = chat.run_pipeline("unitdspr.ns", events.append)

        types = [event["type"] for event in events]
        self.assertEqual(types[0], "status")
        self.assertIn("scan", types)
        self.assertIn("snapshot", types)
        self.assertIn("agent_report", types)
        self.assertEqual(types[-1], "decision")
        self.assertEqual(decision.action, "REJECT")
        self.assertFalse(decision.model_ok)
        final = events[-1]
        self.assertEqual(final["action"], "REJECT")
        self.assertTrue(final["paper_only"])
        self.assertIn("market_regime", final["reject_reasons"][0])

    def test_pipeline_reports_no_data_without_raising(self):
        events: list[dict] = []
        with patch("engine.scanner.scan", return_value={"results": []}), \
             patch.object(chat, "_last_scan_row", return_value=(None, {})):
            decision = chat.run_pipeline("NOPE", events.append)
        self.assertIsNone(decision)
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("No analyzable data", events[-1]["message"])


class ChatServiceTests(unittest.TestCase):
    def test_pipeline_events_stream_and_finish_ok(self):
        def pipeline(symbol, emit):
            emit({"type": "status", "message": "scanning"})
            emit({"type": "agent_report", "agent_id": "risk_analyst",
                  "agent_name": "Risk Analyst",
                  "summary": "The spreadsheet says no."})
            return {"ok": True}

        service = chat.AgentChatService(
            pipeline=pipeline,
            config_provider=lambda: {"enabled": True, "paper_only": True},
            cooldown_seconds=0,
        )
        started = service.start("unitdspr.ns")
        self.assertEqual(started["symbol"], "UNITDSPR")
        self.assertTrue(started["paper_only"])

        events = _collect(service, started["session_id"])
        types = [event["type"] for event in events]
        self.assertEqual(types[0], "session")
        self.assertIn("agent_report", types)
        self.assertEqual(types[-1], "done")
        self.assertTrue(events[-1]["ok"])

    def test_sse_frames_are_parseable_and_end_with_done(self):
        def pipeline(symbol, emit):
            emit({"type": "status", "message": "hello"})
            return True

        service = chat.AgentChatService(
            pipeline=pipeline,
            config_provider=lambda: {"enabled": True},
            cooldown_seconds=0,
        )
        started = service.start("ITC")
        session = service.get(started["session_id"])
        deadline = time.monotonic() + 5
        while not session.done and time.monotonic() < deadline:
            time.sleep(0.01)

        frames = list(chat.sse_frames(session))
        payloads: list[dict] = []
        for frame in frames:
            for line in frame.splitlines():
                if line.startswith("data: "):
                    payloads.append(json.loads(line[6:]))
        self.assertGreaterEqual(len(payloads), 3)
        self.assertEqual(payloads[-1]["type"], "done")
        self.assertTrue(payloads[-1]["ok"])
        ids = [event["seq"] for event in payloads]
        self.assertEqual(ids, sorted(ids))

    def test_ai_mode_off_is_refused(self):
        service = chat.AgentChatService(
            pipeline=lambda symbol, emit: True,
            config_provider=lambda: {"enabled": False},
        )
        with self.assertRaises(chat.ChatUnavailable) as raised:
            service.start("ITC")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("AI mode is off", str(raised.exception))

    def test_invalid_symbol_is_rejected(self):
        service = chat.AgentChatService(
            pipeline=lambda symbol, emit: True,
            config_provider=lambda: {"enabled": True},
        )
        for symbol in ("", "  ", "../etc/passwd", "A" * 61):
            with self.assertRaises(chat.ChatUnavailable) as raised:
                service.start(symbol)
            self.assertEqual(raised.exception.status_code, 400)

    def test_only_one_live_run_at_a_time(self):
        release = threading.Event()

        def pipeline(symbol, emit):
            emit({"type": "status", "message": "working"})
            release.wait(5)
            return True

        service = chat.AgentChatService(
            pipeline=pipeline,
            config_provider=lambda: {"enabled": True},
            cooldown_seconds=0,
        )
        first = service.start("ITC")
        with self.assertRaises(chat.ChatUnavailable) as raised:
            service.start("INFY")
        self.assertEqual(raised.exception.status_code, 409)
        release.set()
        events = _collect(service, first["session_id"])
        self.assertEqual(events[-1]["type"], "done")
        self.assertTrue(events[-1]["ok"])

    def test_secret_text_is_redacted(self):
        text = chat.clean('api_key: super-secret Authorization: Bearer abc.def')
        self.assertNotIn("super-secret", text)
        self.assertNotIn("abc.def", text)
        self.assertIn("[REDACTED]", text)

    def test_stream_events_are_redacted(self):
        session = chat.ChatSession("ITC")
        session.emit({"type": "agent_report",
                      "summary": "api_key: leaked-secret Bearer abc.def"})
        _seq, event = session.events[0]
        self.assertNotIn("leaked-secret", event["summary"])
        self.assertNotIn("abc.def", event["summary"])
        self.assertIn("[REDACTED]", event["summary"])

    def test_router_exposes_chat_routes(self):
        from engine.intelligence.api import router

        paths = {getattr(route, "path", "") for route in router.routes}
        self.assertIn("/api/intelligence/chat/start", paths)
        self.assertIn("/api/intelligence/chat/status", paths)
        self.assertIn("/api/intelligence/chat/{session_id}/stream", paths)


if __name__ == "__main__":
    unittest.main()
