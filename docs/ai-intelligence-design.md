# Autonomous Runtime Intelligence Layer — Design Checkpoint

Branch: `main` (developed on `ai-intelligence`) · Baseline commit: `bc2136f` · Status: Phases 1–10 implemented, independently reviewed, merged to `main`, pushed and auto-deployed on Render. Default mode remains Normal (`intelligence.enabled: false`); AI mode is an explicit operator switch.

## 1. Baseline preserved

- Existing FastAPI backend, React/Vite frontend, SQLite ledger, Groww read-only data client, paper broker, scanner, scoring and V4 config remain in place.
- Baseline invariants: score maximum 100, minimum entry score 85, minimum R:R 2.0, LONG/SHORT configured, paper-only execution, 5× intraday leverage, capital ₹10,000, risk ₹100, daily loss cap ₹300.
- No real order endpoint, credential access, or live execution path is added.
- Note: current runtime implements LONG entries only; `short_entry` is configured but not executed. The intelligence layer must not silently create live shorts.

## 2. Architecture

```mermaid
flowchart LR
  subgraph Existing[Existing Groww Algo]
    M[marketdata / Groww read-only + Yahoo]
    S[scanner + scoring]
    E[AlgoEngine loop]
    R[strategy rules + risk gates]
    B[paper broker / SQLite ledger]
    UI[React dashboard]
  end

  subgraph Intelligence[Separate runtime intelligence layer]
    SN[Validated market snapshot]
    MA[Model adapter: free hosted / local]
    A1[Market Regime Analyst]
    A2[Stock Scanner]
    A3[Technical Analyst]
    A4[Strategy Analyst]
    A5[Adversarial Critic]
    A6[Risk Analyst]
    DB[Structured debate engine]
    RE[Deterministic rule engine]
    OR[Trade decision orchestrator]
    EV[Strategy evolution engine]
    SC[24x7 research scheduler]
    ST[Separate intelligence SQLite]
  end

  M --> SN
  S --> SN
  SN --> A1 & A2 & A3 & A4 & A5 & A6
  MA --> A1 & A2 & A3 & A4 & A5 & A6
  A1 & A2 & A3 & A4 & A5 & A6 --> DB
  DB --> OR
  RE --> OR
  OR -->|advisory gate before broker.buy| E
  E --> B
  B --> UI
  OR --> ST
  EV --> ST
  SC --> EV
  EV -->|validated version only| S
```

## 3. File ownership (parallel work guard)

| Owner | Files |
|---|---|
| Core backend | `engine/intelligence/models.py`, `storage.py`, `config.py`, `snapshot.py`, `rule_engine.py`, `debate.py`, `orchestrator.py` |
| Model layer | `engine/intelligence/model_adapter.py`, `prompts.py`, `agents.py` |
| Evolution/runtime | `engine/intelligence/evolution.py`, `scheduler.py`, `api.py` |
| Integration | `engine/api.py`, `engine/engine.py`, `config/strategy.yaml`, `config/strategy.default.yaml`, `requirements.txt`, `README.md` |
| Frontend | `frontend/src/**`, `frontend/dist/**` |
| QA/Security | `tests/**`, `docs/ai-testing-report.md`, `docs/ai-security-audit.md` |

No two implementation agents may edit the same file. Shared contracts in `models.py` are frozen before parallel work starts.

## 4. Config block

Add `intelligence:` to both strategy YAML files:

- `enabled`, `require_for_entries`, `fail_closed`
- `model`: provider, base_url, model, api_key_env, timeout, retries, rate limit, structured output, `allow_paid: false`, optional local endpoint
- `debate`: rounds, critical objection blocks
- `evolution`: research enabled, interval, auto-promote, validation thresholds, shadow sessions
- `scheduler`: holiday file, research 24×7, market hours only for scanning/execution
- `storage`: separate SQLite path

Secrets are referenced by environment-variable name only. They are never copied into config, prompts, model messages, logs, API responses or the browser.

## 5. Contracts

### MarketSnapshot
Timestamped IST snapshot built only from real `scanner.scan()`, `marketdata.health()`, ledger and config reads. Every candidate carries `source`, `as_of`, `delayed`, `data_stale`, `quote_age_sec`, `score`, `buckets`, indicators and deterministic `build_entry` result. Missing values are explicit `None` plus `missing_information`.

### AgentReport
`agent_id`, `agent_name`, `symbol`, `snapshot_id`, `ts`, `status`, `summary`, `evidence[]`, `missing_information[]`, `uncertainty` 0..1, `confidence` 0..1, `objections[]`, `recommendation` (`support|oppose|abstain`). Agents cannot invent prices or guarantee returns.

### DebateResult
All six reports, rounds, resolved/unresolved objections, consensus score, and `decision` (`approve_candidate|reject_candidate|insufficient_evidence`). Any unresolved critical objection rejects a new candidate. The Critic and Risk Analyst run independently and cannot be suppressed by other agents.

### RuleDecision
Deterministic checks only: market session, data freshness, entry conditions, score ≥85, R:R ≥2, valid stop/target, position size, exposure, daily loss, duplicate position. Calls existing `strategy.build_entry`, `strategy.can_open_new`, `broker.size_position` and ledger reads. It never calls `broker.buy`.

### TradeDecision
Final orchestrator output: `ALLOW|REJECT`, `snapshot_id`, `debate`, `rule_decision`, rationale, model usage IDs and timestamp. Persisted before execution.

## 6. Safety rules

1. Existing positions continue to be managed by deterministic `AlgoEngine.manage_positions()` and `strategy.check_exit()` even if AI fails.
2. If `require_for_entries=true` and model/debate/rule analysis fails or is incomplete, new entries are rejected. No silent fallback to unassisted trading.
3. Agents receive sanitized snapshots only. No credentials, environment variables, broker clients or order methods are exposed.
4. Agents cannot change risk limits, execution permissions, kill switches or active config.
5. Strategy evolution is deterministic code, not an LLM action. No candidate is promoted unless walk-forward, out-of-sample, cost/slippage and shadow checks pass. Previous active version is always retained for rollback.
6. A single additive hook in `AlgoEngine.run_scan()` asks the orchestrator for a decision before `broker.buy`. With `intelligence.enabled=false`, existing behavior is byte-identical.

## 7. Phase gates and evidence

- Phase 2: config + storage + model adapter contracts; evidence: config parse, schema smoke, health check with invalid key.
- Phase 3: six agents produce schema-valid reports on a fixture snapshot; evidence: unit tests.
- Phase 4: debate rejects on critical objection; evidence: unit test.
- Phase 5: rule engine rejects stale data, low score, duplicate, daily loss; evidence: unit tests.
- Phase 6: orchestrator hook integrated; evidence: scratch-DB smoke showing reject on AI failure and paper-only buy path unchanged.
- Phase 7: evolution validates a synthetic candidate, promotes only after checks, rolls back; evidence: tests.
- Phase 8: new Intelligence tab and API; evidence: build + browser smoke.
- Phase 9: no production deploy until QA/security sign-off and explicit approval.
- Phase 10: validation reports and known limitations.

## 8. Implementation checkpoint

All modules in section 3 are present. The additive router is mounted at
`/api/intelligence/*`, the single engine hook sits immediately before
`broker.buy`, and the layer remains disabled in both strategy YAML files.

Post-audit hardening included in this checkpoint:

- Debate always requires the Critic and Risk Analyst roles; opaque agent IDs
  cannot stand in for them. Orchestrator passes the canonical agent IDs.
- Promotion re-checks the safe-path diff against the active baseline and the
  atomic writer repeats the absolute safety guard. A baseline is seeded before
  the first promotion, and `strategy.yaml.bak` has a validated restore path.
- Model credentials may only come from `GROQ_API_KEY` or
  `INTELLIGENCE_MODEL_API_KEY`; hosted endpoints are restricted to the
  allowlisted free providers, and localhost endpoints never receive the hosted
  credential. The Settings YAML editor applies the same validation.
- Strategy-changing POST routes require `INTELLIGENCE_ADMIN_TOKEN` through the
  `X-Intelligence-Token` header. With no token configured, mutation is disabled.
- Any orchestration exception rejects a new entry, even if
  `require_for_entries` is false. Only the explicit disabled path leaves the
  original deterministic behavior unchanged.
- API evidence responses are scrubbed for secret-like fields while preserving
  numeric token counts.

See `docs/ai-security-audit.md` and `docs/ai-testing-report.md` for the
independent reviews and final verdicts.
