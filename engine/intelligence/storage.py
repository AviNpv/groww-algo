"""Separate SQLite store for runtime intelligence evidence.

This is intentionally NOT the trading ledger. It never stores credentials and
never changes cash, positions, P&L or order state.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from engine import settings
from engine.intelligence.models import (
    AgentReport,
    DebateResult,
    ExecutionLog,
    ModelUsage,
    RuleDecision,
    StrategyVersion,
    TradeDecision,
    new_id,
)

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_reports (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    symbol TEXT,
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    confidence REAL NOT NULL,
    uncertainty REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_reports_snapshot ON agent_reports(snapshot_id);

CREATE TABLE IF NOT EXISTS debates (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    decision TEXT NOT NULL,
    consensus_score REAL NOT NULL,
    unresolved_critical INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_debates_symbol ON debates(symbol, ts DESC);

CREATE TABLE IF NOT EXISTS rule_decisions (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    allowed INTEGER NOT NULL,
    reasons TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_decisions (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    rationale TEXT NOT NULL,
    model_ok INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_decisions_ts ON trade_decisions(ts DESC);

CREATE TABLE IF NOT EXISTS model_usage (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    ok INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    total_tokens INTEGER,
    error TEXT,
    correlation_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_ts ON model_usage(ts DESC);

CREATE TABLE IF NOT EXISTS execution_logs (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    symbol TEXT,
    event TEXT NOT NULL,
    decision_id TEXT,
    order_id INTEGER,
    detail TEXT NOT NULL,
    paper_only INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_ai_exec_ts ON execution_logs(ts DESC);

CREATE TABLE IF NOT EXISTS strategy_versions (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    status TEXT NOT NULL,
    yaml TEXT NOT NULL,
    params TEXT NOT NULL,
    metrics TEXT NOT NULL,
    parent_version_id TEXT,
    rationale TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_versions_status ON strategy_versions(status, ts DESC);
"""


def db_path() -> Path:
    configured = settings.cfg("intelligence", "storage", "db_path",
                              default="data/intelligence.sqlite")
    path = Path(str(configured))
    if not path.is_absolute():
        path = settings.BASE_DIR / path
    return path


@contextmanager
def _conn():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        # Fresh deployments have no intelligence database yet. Every read and
        # write goes through this helper, so creating the schema here makes the
        # empty store self-initializing without any explicit bootstrap step.
        with _lock:
            conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _lock, _conn() as conn:
        conn.executescript(SCHEMA)


def _json(model) -> str:
    return model.model_dump_json()


def save_report(report: AgentReport) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO agent_reports"
            " (id,ts,snapshot_id,symbol,agent_id,status,recommendation,confidence,uncertainty,payload)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (report.agent_id + ":" + report.snapshot_id + ":" + (report.symbol or "-"),
             report.ts, report.snapshot_id, report.symbol, report.agent_id, report.status,
             report.recommendation, report.confidence, report.uncertainty, _json(report)),
        )


def save_debate(debate: DebateResult) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO debates"
            " (id,ts,snapshot_id,symbol,decision,consensus_score,unresolved_critical,payload)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (f"{debate.symbol}:{debate.snapshot_id}", debate.ts, debate.snapshot_id,
             debate.symbol, debate.decision, debate.consensus_score,
             int(bool(debate.unresolved_critical)), _json(debate)),
        )


def save_rule_decision(decision: RuleDecision) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO rule_decisions (id,ts,snapshot_id,symbol,allowed,reasons,payload)"
            " VALUES (?,?,?,?,?,?,?)",
            (f"rules:{new_id('rule')}", decision.ts,
             decision.snapshot_id, decision.symbol, int(decision.allowed),
             json.dumps(decision.reasons), _json(decision)),
        )


def save_trade_decision(decision: TradeDecision) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO trade_decisions"
            " (id,ts,snapshot_id,symbol,action,rationale,model_ok,payload)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (decision.decision_id, decision.ts, decision.snapshot_id, decision.symbol,
             decision.action, decision.rationale, int(decision.model_ok), _json(decision)),
        )


def save_model_usage(usage: ModelUsage) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO model_usage"
            " (id,ts,provider,model,ok,latency_ms,total_tokens,error,correlation_id)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (usage.usage_id, usage.ts, usage.provider, usage.model, int(usage.ok),
             usage.latency_ms, usage.total_tokens, usage.error, usage.correlation_id),
        )


def save_execution_log(entry: ExecutionLog) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO execution_logs"
            " (id,ts,symbol,event,decision_id,order_id,detail,paper_only)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (entry.event_id, entry.ts, entry.symbol, entry.event, entry.decision_id,
             entry.order_id, entry.detail, int(entry.paper_only)),
        )


def save_version(version: StrategyVersion) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO strategy_versions"
            " (id,ts,status,yaml,params,metrics,parent_version_id,rationale)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (version.version_id, version.ts, version.status, version.yaml,
             json.dumps(version.params), json.dumps(version.metrics),
             version.parent_version_id, version.rationale),
        )


def set_version_status(version_id: str, status: str) -> None:
    with _lock, _conn() as conn:
        conn.execute("UPDATE strategy_versions SET status = ? WHERE id = ?", (status, version_id))


def _rows(query: str, args: tuple = ()) -> list[dict]:
    with _lock, _conn() as conn:
        return [dict(row) for row in conn.execute(query, args).fetchall()]


def latest_decisions(limit: int = 20, symbol: str | None = None) -> list[dict]:
    if symbol:
        rows = _rows(
            "SELECT * FROM trade_decisions WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
            (symbol, limit),
        )
    else:
        rows = _rows("SELECT * FROM trade_decisions ORDER BY ts DESC LIMIT ?", (limit,))
    for row in rows:
        row["payload"] = json.loads(row["payload"])
    return rows


def reports_for_snapshot(snapshot_id: str) -> list[dict]:
    rows = _rows(
        "SELECT payload FROM agent_reports WHERE snapshot_id = ? ORDER BY agent_id",
        (snapshot_id,),
    )
    return [json.loads(r["payload"]) for r in rows]


def latest_debate(symbol: str | None = None) -> dict | None:
    if symbol:
        rows = _rows("SELECT payload FROM debates WHERE symbol = ? ORDER BY ts DESC LIMIT 1", (symbol,))
    else:
        rows = _rows("SELECT payload FROM debates ORDER BY ts DESC LIMIT 1")
    return json.loads(rows[0]["payload"]) if rows else None


def versions(limit: int = 30) -> list[dict]:
    rows = _rows("SELECT id,ts,status,params,metrics,parent_version_id,rationale"
                 " FROM strategy_versions ORDER BY ts DESC LIMIT ?", (limit,))
    for row in rows:
        row["params"] = json.loads(row["params"] or "{}")
        row["metrics"] = json.loads(row["metrics"] or "{}")
    return rows


def get_version(version_id: str) -> dict | None:
    rows = _rows("SELECT * FROM strategy_versions WHERE id = ?", (version_id,))
    if not rows:
        return None
    row = rows[0]
    row["params"] = json.loads(row["params"] or "{}")
    row["metrics"] = json.loads(row["metrics"] or "{}")
    return row


def active_version() -> dict | None:
    rows = _rows("SELECT * FROM strategy_versions WHERE status = 'active' ORDER BY ts DESC LIMIT 1")
    if not rows:
        return None
    row = rows[0]
    row["params"] = json.loads(row["params"] or "{}")
    row["metrics"] = json.loads(row["metrics"] or "{}")
    return row


def usage(limit: int = 50) -> list[dict]:
    return _rows("SELECT * FROM model_usage ORDER BY ts DESC LIMIT ?", (limit,))


def executions(limit: int = 50) -> list[dict]:
    return _rows("SELECT * FROM execution_logs ORDER BY ts DESC LIMIT ?", (limit,))


def summary() -> dict:
    with _lock, _conn() as conn:
        def count(table: str) -> int:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return {
            "db_path": str(db_path()),
            "agent_reports": count("agent_reports"),
            "debates": count("debates"),
            "rule_decisions": count("rule_decisions"),
            "trade_decisions": count("trade_decisions"),
            "model_calls": count("model_usage"),
            "executions": count("execution_logs"),
            "strategy_versions": count("strategy_versions"),
        }
