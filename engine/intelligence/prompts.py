"""System prompts and sanitized user payloads for the six advisory agents.

Snapshots are the only source of facts. Payloads contain plain JSON built from
the frozen snapshot contracts; credentials, broker clients, environment values
and writable configuration are never included.
"""
from __future__ import annotations

from typing import Any

from engine.intelligence.models import CandidateSnapshot, MarketSnapshot

AGENT_IDS: tuple[str, ...] = (
    "market_regime",
    "stock_scanner",
    "technical_analyst",
    "strategy_analyst",
    "adversarial_critic",
    "risk_analyst",
)

AGENT_NAMES: dict[str, str] = {
    "market_regime": "Market Regime Analyst",
    "stock_scanner": "Stock Scanner",
    "technical_analyst": "Technical Analyst",
    "strategy_analyst": "Strategy Analyst",
    "adversarial_critic": "Adversarial Critic",
    "risk_analyst": "Risk Analyst",
}

_COMMON_RULES = """\
HARD RULES:
- Evidence: cite only `ref` values that appear in the supplied payload (see
  `allowed_evidence_refs`). Every factual claim needs such a reference. Never
  cite, infer or invent data that the payload does not contain.
- Missing information: list every gap explicitly in `missing_information`. Use
  an empty list only when nothing relevant is absent.
- Uncertainty: always set both `uncertainty` and `confidence` to numbers from
  0.0 to 1.0 that honestly reflect how strong the supplied evidence is.
- No fabrication: do not fabricate, estimate, extrapolate or guess any price,
  indicator, ratio, score, volume, date or timestamp.
- No guaranteed performance: never claim or guarantee profits, returns, win
  rates or any future performance.
- Out of scope: never discuss credentials, API keys, environment variables,
  secrets, broker clients, writable configuration, order placement or order
  execution. You are advisory only and cannot place orders.
- If evidence is insufficient, abstain and say so plainly.
"""

_OUTPUT_CONTRACT = """\
OUTPUT CONTRACT:
Respond with exactly one structured JSON object (no prose, no markdown) that
matches the AgentReport fields: agent_id, agent_name, symbol, snapshot_id, ts,
status, summary, evidence, missing_information, uncertainty, confidence,
objections, recommendation, model, usage_id. `evidence` items use source, key,
value, note and ref. `objections` items use severity (critical|major|minor),
category, message, evidence_refs, requires_resolution and resolved.
`recommendation` is exactly one of support|oppose|abstain. Take snapshot_id and
symbol only from the payload. The runtime deterministically overwrites
agent_id, agent_name, symbol, snapshot_id, ts, status, model and usage_id, so
never guess those values from outside the payload.
"""


def _prompt(role: str, focus: str) -> str:
    return (
        f"You are the {role} in a paper-trading advisory debate. "
        "Analyse only the supplied market snapshot and candidate.\n"
        f"YOUR FOCUS: {focus}\n\n"
        f"{_COMMON_RULES}\n{_OUTPUT_CONTRACT}"
    )


SYSTEM_PROMPTS: dict[str, str] = {
    "market_regime": _prompt(
        "Market Regime Analyst",
        "Classify the current regime (trending, range-bound or risky) using "
        "only market_session, feed state and the supplied candidate context. "
        "Call out delayed feeds, stale quotes and non-closed candles explicitly.",
    ),
    "stock_scanner": _prompt(
        "Stock Scanner",
        "Judge whether the supplied candidate is genuinely supported by its "
        "score, confirmations, buckets and reasons. Do not rank or mention "
        "symbols that are absent from the payload.",
    ),
    "technical_analyst": _prompt(
        "Technical Analyst",
        "Evaluate only the supplied indicator values, patterns, closed-candle "
        "status and price/action/stop/target fields. Treat missing indicator "
        "values as missing information rather than neutral signals.",
    ),
    "strategy_analyst": _prompt(
        "Strategy Analyst",
        "Assess alignment between the candidate's deterministic score, buckets, "
        "confirmations and risk_reward and the supplied read-only baseline. "
        "Never propose changing rules, limits or permissions.",
    ),
    "adversarial_critic": _prompt(
        "Adversarial Critic",
        "Independently search for contradictions, stale data, weak evidence, "
        "overfitting and reasons the candidate must be rejected. Your objections "
        "cannot be suppressed by the other agents; raise a critical objection "
        "when evidence is missing or contradictory.",
    ),
    "risk_analyst": _prompt(
        "Risk Analyst",
        "Assess data freshness, account exposure, open positions, risk_amount, "
        "stop, target and risk_reward. Emphasise capital preservation and "
        "abstain whenever downside evidence is incomplete.",
    ),
}

_SECRET_MARKERS = (
    "secret",
    "password",
    "passwd",
    "credential",
    "api_key",
    "apikey",
    "private_key",
    "access_key",
    "token",
    "authorization",
    "cookie",
    "signature",
    "bearer",
)
_BASELINE_KEYS = (
    "paper_only",
    "require_for_entries",
    "fail_closed",
    "min_score",
    "min_entry_score",
    "min_rr",
    "min_risk_reward",
    "risk_per_trade",
    "risk_pct",
    "risk_amount",
    "max_positions",
    "daily_loss_cap",
    "daily_loss_limit",
)
_DROP = object()


def _sanitize(value: Any) -> Any:
    """Keep JSON-safe scalars/containers; drop secret-looking keys and objects."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if any(marker in name.lower() for marker in _SECRET_MARKERS):
                continue
            result = _sanitize(item)
            if result is not _DROP:
                cleaned[name] = result
        return cleaned
    if isinstance(value, (list, tuple)):
        items: list[Any] = []
        for item in value:
            result = _sanitize(item)
            if result is not _DROP:
                items.append(result)
        return items
    return _DROP


def _leaf_refs(prefix: str, value: Any, refs: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _leaf_refs(f"{prefix}.{key}", item, refs)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _leaf_refs(f"{prefix}[{index}]", item, refs)
    else:
        refs.append(prefix)


def build_user_payload(
    snapshot: MarketSnapshot,
    candidate: CandidateSnapshot | None,
    agent_id: str,
) -> dict:
    """Build a JSON-only payload whose refs are the only citable evidence."""
    baseline = {
        key: snapshot.config_baseline[key]
        for key in _BASELINE_KEYS
        if key in snapshot.config_baseline
    }
    snapshot_data = {
        "snapshot_id": snapshot.snapshot_id,
        "ts": snapshot.ts,
        "timezone": snapshot.timezone,
        "market_session": _sanitize(snapshot.market_session),
        "feed": _sanitize(snapshot.feed.model_dump()),
        "account": _sanitize(snapshot.account.model_dump()),
        "positions": _sanitize([p.model_dump() for p in snapshot.positions]),
        "config_baseline": _sanitize(baseline),
    }
    candidate_data = _sanitize(candidate.model_dump()) if candidate is not None else None

    refs: list[str] = []
    _leaf_refs("snapshot", snapshot_data, refs)
    if candidate_data is not None:
        _leaf_refs("candidate", candidate_data, refs)

    return {
        "agent_id": agent_id,
        "agent_name": AGENT_NAMES.get(agent_id, agent_id),
        "symbol": candidate.symbol if candidate is not None else None,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot": snapshot_data,
        "candidate": candidate_data,
        "allowed_evidence_refs": list(dict.fromkeys(refs)),
        "instruction": (
            "Cite only refs from allowed_evidence_refs, list gaps in "
            "missing_information and return one AgentReport JSON object."
        ),
    }
