import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.intelligence import config as ai_config
from engine.intelligence import storage
from engine.intelligence.models import (
    AgentReport,
    DebateResult,
    ExecutionLog,
    Objection,
    StrategyVersion,
    TradeDecision,
)


class ContractTests(unittest.TestCase):
    def test_critical_objection_blocks(self):
        report = AgentReport(
            agent_id="adversarial_critic",
            agent_name="Adversarial Critic",
            symbol="TEST",
            snapshot_id="snap-1",
            ts="2026-11-22T10:00:00+05:30",
            summary="Reject",
            recommendation="oppose",
            objections=[Objection(severity="critical", category="data", message="stale quote")],
        )
        self.assertTrue(report.has_critical_objection)

    def test_config_forces_paper_and_no_paid(self):
        cfg = ai_config.config()
        self.assertTrue(cfg["paper_only"])
        self.assertFalse(cfg["model"]["allow_paid"])
        self.assertEqual(cfg["model"]["provider"], "openai_compatible")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "intelligence.sqlite"
        real_cfg = storage.settings.cfg

        def fake_cfg(*keys, default=None):
            if keys == ("intelligence", "storage", "db_path"):
                return str(self.db)
            return real_cfg(*keys, default=default)

        self.patcher = patch.object(storage.settings, "cfg", side_effect=fake_cfg)
        self.patcher.start()
        storage.init_db()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_report_debate_decision_version_roundtrip(self):
        report = AgentReport(
            agent_id="risk_analyst",
            agent_name="Risk Analyst",
            symbol="TEST",
            snapshot_id="snap-1",
            ts="2026-11-22T10:00:00+05:30",
            summary="Risk acceptable",
            recommendation="support",
            confidence=0.8,
            uncertainty=0.2,
        )
        storage.save_report(report)
        self.assertEqual(len(storage.reports_for_snapshot("snap-1")), 1)

        debate = DebateResult(
            symbol="TEST",
            snapshot_id="snap-1",
            ts=report.ts,
            rounds=2,
            reports=[report],
            consensus_score=0.8,
            decision="approve_candidate",
            rationale="Supported",
        )
        storage.save_debate(debate)
        latest = storage.latest_debate("TEST")
        self.assertEqual(latest["decision"], "approve_candidate")

        decision = TradeDecision(
            symbol="TEST",
            action="ALLOW",
            ts=report.ts,
            snapshot_id="snap-1",
            rationale="All checks passed",
            model_ok=True,
            debate=debate,
        )
        storage.save_trade_decision(decision)
        rows = storage.latest_decisions(symbol="TEST")
        self.assertEqual(rows[0]["action"], "ALLOW")

        version = StrategyVersion(
            ts=report.ts,
            status="candidate",
            yaml="app:\n  version: '4.0'\n",
            params={"exit.target.target_r_mult": 2.0},
            rationale="test candidate",
        )
        storage.save_version(version)
        stored = storage.get_version(version.version_id)
        self.assertEqual(stored["status"], "candidate")
        self.assertEqual(stored["params"]["exit.target.target_r_mult"], 2.0)

    def test_fresh_store_is_initialized_by_reads(self):
        self.db.unlink(missing_ok=True)
        self.assertFalse(self.db.exists())
        self.assertEqual(storage.latest_decisions(), [])
        self.assertEqual(storage.summary()["agent_reports"], 0)
        self.assertTrue(self.db.exists())

    def test_execution_log_is_paper_only(self):
        entry = ExecutionLog(
            ts="2026-11-22T10:00:00+05:30",
            symbol="TEST",
            event="intelligence_decision",
            detail="ALLOW",
        )
        storage.save_execution_log(entry)
        rows = storage.executions(limit=1)
        self.assertEqual(rows[0]["paper_only"], 1)
        self.assertTrue(entry.paper_only)


if __name__ == "__main__":
    unittest.main()
