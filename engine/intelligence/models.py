"""Frozen structured contracts for the runtime intelligence layer.

All timestamps are IST ISO strings. All agents exchange these models only; no
agent receives credentials, broker clients, writable config handles or order
methods.
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Evidence(BaseModel):
    source: str
    key: str
    value: Any | None = None
    note: str | None = None
    ref: str | None = None


class Objection(BaseModel):
    id: str = Field(default_factory=lambda: new_id("obj"))
    severity: Literal["critical", "major", "minor"]
    category: str
    message: str
    evidence_refs: list[str] = Field(default_factory=list)
    requires_resolution: bool = True
    resolved: bool = False
    resolution_note: str | None = None


class AgentReport(BaseModel):
    agent_id: str
    agent_name: str
    symbol: str | None = None
    snapshot_id: str
    ts: str
    status: Literal["ok", "insufficient_data", "error"] = "ok"
    summary: str
    evidence: list[Evidence] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    uncertainty: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    objections: list[Objection] = Field(default_factory=list)
    recommendation: Literal["support", "oppose", "abstain"] = "abstain"
    model: str | None = None
    usage_id: str | None = None

    @property
    def has_critical_objection(self) -> bool:
        return any(
            o.severity == "critical" and o.requires_resolution and not o.resolved
            for o in self.objections
        )


class FeedState(BaseModel):
    active: str = "unknown"
    source: str | None = None
    groww_ok: bool = False
    state: str | None = None
    message: str | None = None
    delayed: bool | None = None
    as_of: str | None = None


class AccountState(BaseModel):
    capital: float
    cash: float
    equity: float
    day_start_equity: float
    margin_blocked: float
    open_positions: int
    realized_pnl: float


class PositionState(BaseModel):
    symbol: str
    qty: int
    avg_price: float
    last_price: float
    stop: float | None = None
    target: float | None = None
    unrealized: float = 0.0
    r_multiple: float | None = None


class CandidateSnapshot(BaseModel):
    symbol: str
    price: float
    score: float
    status: str
    confirmations: int
    buckets: dict[str, Any] = Field(default_factory=dict)
    indicators: dict[str, Any] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    patterns: list[Any] = Field(default_factory=list)
    source: str
    delayed: bool
    data_stale: bool
    quote_age_sec: float | None = None
    as_of: str | None = None
    closed_candle: bool | None = None
    ts: str
    action: str | None = None
    stop: float | None = None
    target: float | None = None
    qty: int | None = None
    risk_amount: float | None = None
    risk_reward: float | None = None
    rejection_reasons: list[str] = Field(default_factory=list)


class MarketSnapshot(BaseModel):
    snapshot_id: str
    ts: str
    timezone: str = "Asia/Kolkata"
    market_session: dict[str, Any] = Field(default_factory=dict)
    feed: FeedState
    account: AccountState
    positions: list[PositionState] = Field(default_factory=list)
    candidates: list[CandidateSnapshot] = Field(default_factory=list)
    config_baseline: dict[str, Any] = Field(default_factory=dict)


class ModelUsage(BaseModel):
    usage_id: str = Field(default_factory=lambda: new_id("usage"))
    provider: str
    model: str
    ts: str
    ok: bool
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    error: str | None = None
    correlation_id: str | None = None


class ModelResult(BaseModel):
    ok: bool
    data: dict[str, Any] | None = None
    usage: ModelUsage
    error: str | None = None


class ModelHealth(BaseModel):
    ok: bool
    provider: str
    model: str
    ts: str
    latency_ms: int | None = None
    message: str
    paid_allowed: bool = False


class DebateResult(BaseModel):
    symbol: str
    snapshot_id: str
    ts: str
    rounds: int
    reports: list[AgentReport]
    unresolved_critical: list[Objection] = Field(default_factory=list)
    consensus_score: float = 0.0
    decision: Literal["approve_candidate", "reject_candidate", "insufficient_evidence"] = "insufficient_evidence"
    rationale: str


class RuleCheck(BaseModel):
    name: str
    ok: bool
    detail: str


class RuleDecision(BaseModel):
    symbol: str
    snapshot_id: str
    ts: str
    allowed: bool
    checks: list[RuleCheck] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    computed: dict[str, Any] = Field(default_factory=dict)


class TradeDecision(BaseModel):
    decision_id: str = Field(default_factory=lambda: new_id("decision"))
    symbol: str
    action: Literal["ALLOW", "REJECT"]
    ts: str
    snapshot_id: str
    rationale: str
    debate: DebateResult | None = None
    rules: RuleDecision | None = None
    model_ok: bool = False
    reject_reasons: list[str] = Field(default_factory=list)


class StrategyVersion(BaseModel):
    version_id: str = Field(default_factory=lambda: new_id("strategy"))
    ts: str
    status: Literal["candidate", "validated", "shadow", "active", "rejected", "rolled_back"]
    yaml: str
    params: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    parent_version_id: str | None = None
    rationale: str = ""


class ExecutionLog(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("exec"))
    ts: str
    symbol: str | None = None
    event: str
    decision_id: str | None = None
    order_id: int | None = None
    detail: str = ""
    paper_only: bool = True
