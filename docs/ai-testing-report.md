# Runtime Intelligence Layer — Independent QA Report

- **Date:** 2026-11-22
- **Repository / branch:** `C:\Users\essve\groww-algo` / `ai-intelligence`
- **Reviewer role:** independent QA subagent (no implementation changes)
- **Interpreter:** `.\.venv\Scripts\python.exe` — Python 3.13.14, pydantic 2.14.3, fastapi 0.162.7
- **Files created (only these two):** `tests/test_intelligence_qa.py`, `docs/ai-testing-report.md`
- **Re-verification:** 2026-11-22, after defect remediation. Sections 1–5 are retained
  unchanged as the historical pre-remediation findings; **Section 6 is the current final
  verification** and supersedes the old conclusion.

## 1. Scope reviewed

All files under `engine/intelligence/` were read (`models.py`, `config.py`, `storage.py`,
`debate.py`, `rule_engine.py`, `orchestrator.py`, `model_adapter.py`, `agents.py`,
`evolution.py`, `scheduler.py`, `snapshot.py`, `prompts.py`, `api.py`, `__init__.py`),
plus:

- `engine/engine.py:12-63` — lazy `_intelligence_gate()` hook and its call site at
  `engine/engine.py:217-227`.
- `engine/api.py:22,39-40,84-113` — router mount and scheduler startup/shutdown.
- `config/strategy.yaml:691-737` — runtime intelligence configuration (read only).
- `frontend/dist/index.html` and `frontend/src/` — build artefact existence.

## 2. Test execution and actual results

Commands (run from the repo root):

```
.\.venv\Scripts\python.exe -m unittest tests.test_intelligence_qa -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

| Run | Tests run | Passed | Failed | Expected failures | Errors |
| --- | ---: | ---: | ---: | ---: | ---: |
| QA suite only (`tests.test_intelligence_qa`) | 54 | **51** | **0** | **3** | 0 |
| Full discovery (QA suite + 4 pre-existing foundation tests) | 58 | **55** | **0** | **3** | 0 |

Final runner line for both runs: `OK (expected failures=3)`.

The three expected failures are deliberate, documented assertions of *desired* behaviour
that currently fail because of real implementation defects (BUG-1, BUG-2, BUG-3 below).
They are marked `@unittest.expectedFailure` so the suite stays green while keeping the
defects reproducible; if the implementation is fixed, unittest will report an
"unexpected success" and the marker should be removed. **No test was skipped.**

### Safety of the test run

- Every intelligence-store operation was redirected to a temporary SQLite file by
  monkeypatching `engine.settings.cfg` at the path `intelligence.storage.db_path`
  (`storage.db_path()` at `storage.py:115-121`).
- `ledger.positions`, `strategy.can_open_new`, `strategy.symbol_trade_count` and
  `broker.size_position` were patched at the `rule_engine` module boundary, so
  `data/algo.sqlite` was never opened by these tests.
- Promotion/rollback tests patch `_atomic_write_strategy` (one test patches it with a
  wrapper that still runs the real read-only validator). A SHA-256 comparison of
  `config/strategy.yaml` inside the rollback test confirmed the file was unchanged.
- Post-run file timestamps were checked: `config/strategy.yaml`,
  `config/strategy.default.yaml` and `data/algo.sqlite` retained their pre-test
  modification times (12:19) while the run finished at 12:24.
- No deployment, commit, network call, real API key, broker order or live model call was made.
- During final verification, a concurrent process rebuilt `frontend/dist` (the referenced
  JS asset changed from `index-DuR4r8FZ.js` to `index-ntcXw2EJ.js`). The frontend tests read
  `dist/index.html` dynamically and were re-run against the new artefacts; both still pass.

## 3. Requirement coverage

| # | Requirement | Result | Representative tests |
| --- | --- | --- | --- |
| 1 | Debate blocks unresolved critical objections from Adversarial Critic / Risk Analyst; `insufficient_evidence` when reports missing | Verified | `test_critic_unresolved_critical_objection_rejects`, `test_risk_analyst_unresolved_critical_objection_rejects`, `test_*_blocks_even_when_aggregation_flag_is_off`, `test_resolved_or_nonblocking_objection_does_not_reject`, `test_missing_reports_report_insufficient_evidence`, `test_missing_critical_role_reports_insufficient_evidence`, `test_error_status_report_is_insufficient_evidence` |
| 2 | RuleEngine rejects stale data, non-Groww/delayed source, score <85, R:R <2, duplicate held symbol, daily-loss/portfolio gate failure, outside market window | Verified | `test_stale_data_is_rejected`, `test_non_groww_or_delayed_source_is_rejected`, `test_score_below_85_is_rejected`, `test_risk_reward_below_2_is_rejected`, `test_duplicate_held_symbol_is_rejected`, `test_duplicate_ledger_position_is_rejected`, `test_daily_loss_gate_failure_is_rejected`, `test_outside_market_window_is_rejected`, `test_config_cannot_lower_score_or_rr_floors`, `test_baseline_candidate_is_allowed` |
| 3 | Orchestrator fails closed on model failure/exception; refuses ALLOW on debate/rule rejection; disabled path unchanged | Verified with one documented caveat (§5) | `test_agent_exception_fails_closed`, `test_error_reports_fail_closed`, `test_debate_rejection_refuses_allow`, `test_debate_insufficient_evidence_refuses_allow`, `test_rule_rejection_refuses_allow`, `test_unresolved_critical_objection_refuses_allow`, `test_exception_in_evaluation_fails_closed`, `test_disabled_path_allows_without_running_any_ai_gate`, `test_engine_hook_*` |
| 4 | ModelAdapter never dumps env vars/secrets, fails cleanly without a key, validates structured JSON, logs usage | Verified | `test_no_environment_dump_in_request_body_or_usage` (asserts exactly one env read, no env names/values in request body or usage), `test_missing_api_key_fails_cleanly_and_logs_usage`, `test_structured_json_is_validated`, `test_secret_is_redacted_from_http_error_usage`, `test_local_endpoint_never_receives_hosted_credential` |
| 5 | Evolution cannot change app.mode, execution mode, safety flags, capital, risk limits, leverage, score floor, R:R below 2; rollback preserves previous version | Verified with one defect (BUG-2) | `test_absolute_safety_guard_rejects_unsafe_documents`, `test_only_safe_paths_diff_is_allowed`, `test_generated_candidates_only_change_safe_leaves`, `test_rollback_restores_previous_version_without_writing_config`, `test_promotion_rejects_unsafe_yaml_without_writing`, `test_promotion_requires_real_shadow_evidence`, `test_promotion_rejects_tampered_capital_or_leverage` (expected failure) |
| 6 | Paper-only: no order endpoint, no broker buy/sell anywhere, execution logs `paper_only=True` | Verified | `test_no_order_endpoint_in_intelligence_router` (POST set is exactly run/promote/rollback), `test_no_broker_order_calls_anywhere_in_intelligence` (AST scan of all 14 `.py` files), `test_execution_log_constructors_default_to_paper_only`, `test_runtime_execution_logs_are_paper_only`, `test_config_hard_forces_paper_only_and_no_paid_model`, `test_router_is_mounted_in_main_app` |
| 7 | `dist/index.html` assets exist; src build inputs exist (existence only) | Verified | `test_dist_references_existing_nonempty_assets`, `test_src_build_inputs_exist` |

## 4. Defects found (implementation NOT changed)

### BUG-1 — Debate silently skips critical-role presence when no role token matches

- **Severity:** medium (fail-open at the debate layer if reports are not produced by the
  fixed `prompts.AGENT_IDS` panel).
- **Location:** `engine/intelligence/debate.py:89-100` (role matching / `if matched_roles:`
  guard), with `engine/intelligence/orchestrator.py:138-145` never passing the available
  `required_report_ids` argument.
- **Observed behaviour:** six `AgentReport`s with agent IDs `agent_0` … `agent_5`, all
  `status="ok"`, no objections, produce `decision="approve_candidate"` with
  `consensus_score=0.95`. `missing_critical` stays empty because `_norm()` matches none of
  `_ALL_ROLE_TOKENS`, so the `if matched_roles:` block is skipped entirely. The same
  mechanism means a partially recognised panel can silently relax the "full panel"
  requirement (`full_panel` is derived from the number of *matched* roles, not the
  configured panel size).
- **Expected behaviour:** a debate without recognisable Critic and Risk Analyst reports
  must return `insufficient_evidence`, regardless of report count.
- **Reproduction:** `test_unrecognised_agent_ids_cannot_satisfy_critical_roles`
  (expected failure).
- **Suggested fix:** always require `CRITICAL_ROLE_TOKENS` roles independent of
  `matched_roles`, and/or have `Orchestrator._run_debate()` pass
  `required_report_ids=prompts.AGENT_IDS` to `DebateEngine.run()`. Derive `full_panel`
  from `min_reports` / the configured agent list rather than from `matched_roles`.

### BUG-2 — `promote()` does not re-check the safe-path diff against the baseline

- **Severity:** high (safety invariant "evolution cannot change capital / leverage / risk
  limits" is enforced only during candidate generation, not at installation time).
- **Location:** `engine/intelligence/evolution.py:526-563`, specifically line 550
  (`_check_absolute_safety(yaml.safe_load(text))`) and line 552
  (`_atomic_write_strategy(text)`). The write path `evolution.py:221-235` →
  `evolution.py:199-218` → `engine/api.py:647-714` only performs absolute/range checks
  (capital ≥ 1, leverage ≤ 10, score 0..100, R:R ≥ 0); it never compares against the
  active document. `_assert_only_safe_changes()` exists (`evolution.py:155-163`) but is
  only called from `_generate_candidates()` (`evolution.py:282-333`).
- **Observed behaviour:** a stored `validated` version whose YAML changes
  `account.initial_capital` to `999999` and `account.intraday_leverage` to `10` (both
  within the main validator's ranges) passes `_check_absolute_safety`, passes
  `engine.api._validate_strategy`, and is promoted. The real validator was invoked in
  the test (via a patched wrapper), so this is not an artefact of bypassing validation.
- **Expected behaviour:** promotion of any stored version must reject changes outside
  `SAFE_PATHS`, including capital, leverage, risk limits, engine/safety flags and times.
- **Reproduction:** `test_promotion_rejects_tampered_capital_or_leverage`
  (expected failure).
- **Suggested fix:** in `promote()`, before `_atomic_write_strategy(text)`, load the
  current baseline (`load_active_yaml()` / the version's parent YAML) and call
  `_assert_only_safe_changes(yaml.safe_load(baseline), yaml.safe_load(text))`; keep
  `_check_absolute_safety()` as an additional, not replacement, guard. Consider pinning
  capital/leverage/risk-limit values absolutely as well.

### BUG-3 — Repeated rule evaluations for one snapshot are silently not persisted

- **Severity:** low (audit/observability only; decisions remain fail-closed).
- **Location:** `engine/intelligence/storage.py:172-180` — plain `INSERT` with the fixed
  key `f"{symbol}:{snapshot_id}:rules"`; `engine/intelligence/rule_engine.py:298-304`
  swallows any persistence exception with `except Exception: pass`.
- **Observed behaviour:** the first `RuleEngine.evaluate()` for a given
  `(symbol, snapshot_id)` writes one row; the second raises a UNIQUE violation that is
  swallowed, leaving only one row in `rule_decisions`. Debate and report saves use
  `INSERT OR REPLACE`, so this is inconsistent within the same store.
- **Expected behaviour:** every evaluation is auditable (or at least the latest one is
  deterministically retained with a visible overwrite).
- **Reproduction:** `test_repeated_rule_decisions_for_same_snapshot_are_persisted`
  (expected failure); `test_rule_decision_is_persisted_to_scratch_store` documents the
  current single-row behaviour.
- **Suggested fix:** use `INSERT OR REPLACE` (matching `save_debate`/`save_report`) or add
  a unique evaluation/decision id to the row key; count/log swallowed persistence errors
  instead of discarding them silently.

### Minor code note (not a failing test)

- `engine/intelligence/debate.py:139` contains an unreachable `elif insufficient:`
  branch (the same condition was already handled at line 131). Dead code, no behavioural
  impact; remove for clarity.

## 5. Known gaps and limitations

1. **Exception path can fail open when explicitly configured to.** With
   `intelligence.require_for_entries = false`, an exception during evaluation returns
   `ALLOW` (`orchestrator.py:323-339`); this is asserted by
   `test_exception_fails_open_when_require_for_entries_is_disabled`. The shipped
   `config/strategy.yaml:693` sets it to `true`, so production is fail-closed, but an
   operator toggle weakens the guarantee. `fail_closed=false` does *not* open the normal
   gate (ALLOW still requires model_ok, rules allowed and debate approval); only the
   exception handler is affected. Consider making exceptions always fail closed.
2. **No live model / real-agent end-to-end test.** ModelAdapter was exercised with a
   mocked `urlopen` and synthetic JSON. Real Groq connectivity, the 70B model's actual
   adherence to the `AgentReport` JSON contract, token/latency telemetry against a real
   endpoint, rate limiting under concurrency and retry/backoff timing were not verified.
   No API key or network access was used.
3. **No end-to-end scan test.** `snapshot.build_snapshot()`, `RuntimeAgents`,
   `IntelligenceScheduler` thread lifecycle and the real `AlgoEngine.run_scan()` loop
   were reviewed statically but not executed (they would require market data/network and
   the production ledger). Tests inject snapshots and synthetic reports instead.
4. **Evolution validation is an approximation.** The engine's own `_LIMITATIONS`
   (`evolution.py:56-70`) states walk-forward uses daily Yahoo bars with a cutoff proxy,
   cannot model 5-minute order flow, may expose a temporary cutoff view to a concurrent
   manual backtest, and that `risk.minimum_risk_reward` mutations are behaviourally inert
   because runtime reads `entry.risk.minimum_risk_reward` / `entry.min_risk_reward`.
   These were not independently reproduced (would need historical data and the backtester).
5. **API checks are introspection-only.** Router paths/methods were inspected in-process;
   no HTTP client requests were made against FastAPI endpoints (status/health/usage
   endpoints, validation errors and `_scrub()` redaction were not exercised). Importing
   `engine.api` emits FastAPI `on_event` DeprecationWarnings (pre-existing, out of scope);
   these are suppressed in the QA module to keep output readable.
6. **Frontend check is existence-only, as requested.** Referenced assets and key `src`
   files are present and non-empty; the JS bundle was not rebuilt, parsed, or compared
   with source content, and no browser rendering was tested.
7. **Rule-engine tests substitute dependencies.** Ledger positions, `can_open_new`,
   `symbol_trade_count`, `size_position` and the market clock are patched. Real paper
   ledger state, real position sizing and real IST session boundaries were not exercised.
8. **Debate consensus calibration untested.** The 0.66 approval threshold and the
   half-weighted uncertainty penalty were only exercised with clean synthetic panels
   (all `uncertainty=0.1`) and a degenerate misuse case (BUG-1). Real-model uncertainty
   distributions could produce unexpected `reject_candidate` rates; no threshold tuning
   evidence is available from this review.
9. **Deduplication of reject reasons is string-based** (`orchestrator.py:360-367`);
   no test covers pathological duplicate messages, and this is not a safety issue.
10. **Test doubles for the debate/rules in orchestrator tests** intentionally bypass the
    real DebateEngine/RuleEngine so each gate can be isolated; the combination of all
    three real components in one live decision was not executed.

## 6. Final verification (post-remediation, current tree)

Sections 1–5 above are kept intact as historical findings. This section records the
independent re-verification of the **current** working tree after remediation.

### Commands and exact results

Run from `C:\Users\essve\groww-algo`:

```
.\.venv\Scripts\python.exe -m compileall -q engine tests scripts
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

| Check | Result |
| --- | --- |
| `compileall -q engine tests scripts` | Exit code 0, no output — all modules compiled. |
| Full discovery run | `Ran 65 tests in 9.861s` → `OK` (repeat run: `Ran 65 tests in 10.788s` → `OK`). Final confirmation run: 65 tests, exit code 0. |
| Passed / failed / errors | **65 passed, 0 failed, 0 errors.** |
| Expected failures / skips | **0 expected failures, 0 skipped.** The runner printed plain `OK` with no `(expected failures=...)` or `(skipped=...)` suffix. |
| Test split | 61 QA tests in `tests/test_intelligence_qa.py` + 4 pre-existing tests in `tests/test_intelligence_foundation.py`. |
| Decorator check | No `@unittest.expectedFailure`, `@unittest.skip`, `@unittest.skipIf`, `@unittest.skipUnless` or other skip decorators remain in `tests/test_intelligence_qa.py`. The stale docstring wording was corrected after this re-verification; no executable decorator was present. |

### Defect remediation verified in the current source

| Defect | Fix observed | Previously failing test, now a normal pass |
| --- | --- | --- |
| **BUG-1** — debate critical-role presence | `engine/intelligence/debate.py:80-104` now always requires critical roles even when no role token matches, and derives `full_panel` from `min_reports` + matched roles rather than silently relaxing the panel. `engine/intelligence/orchestrator.py:136-151` passes `required_report_ids` from `prompts.AGENT_IDS` (with a compatibility fallback). | `test_unrecognised_agent_ids_cannot_satisfy_critical_roles` |
| **BUG-2** — promotion safe-path diff | `engine/intelligence/evolution.py:594-601` now calls `_assert_only_safe_changes()` against the active baseline YAML in `promote()`, in addition to `_check_absolute_safety()`, before `_atomic_write_strategy()`. | `test_promotion_rejects_tampered_capital_or_leverage` |
| **BUG-3** — repeated rule decisions lost | `engine/intelligence/storage.py:173-181` now keys each rule decision with a fresh `rules:{new_id('rule')}` id, so repeated evaluations for one snapshot each persist a row. | `test_repeated_rule_decisions_for_same_snapshot_are_persisted` |

Additional hardening confirmed: the orchestrator exception handler
(`engine/intelligence/orchestrator.py:329-338`) always emits a `REJECT` decision, even when
`require_for_entries`/`fail_closed` are disabled; the old fail-open branch is gone. This
closes historical limitation §5.1 and is covered by
`test_exception_always_fails_closed_even_if_require_flag_is_disabled`.

### New security tests (7, all passing)

`SecurityRemediationQATests` in `tests/test_intelligence_qa.py`:

1. `test_admin_token_is_required_and_constant_time_checked` — accepts the configured
   `INTELLIGENCE_ADMIN_TOKEN`, returns 401 for a wrong token (constant-time
   `secrets.compare_digest` in `engine/intelligence/api.py:82-93`) and 503 when unset.
2. `test_evolution_route_enforces_admin_gate` — `intelligence_evolution_run` rejects a
   request without the token (401).
3. `test_model_adapter_blocks_unapproved_env_and_endpoint` — an `AWS_SECRET_ACCESS_KEY` /
   `https://evil.example` configuration causes zero environment reads and zero HTTP calls.
4. `test_strategy_editor_rejects_credential_redirection` — `_validate_strategy()` returns
   400 for the same redirection attempt through the YAML editor.
5. `test_ensure_baseline_seeds_one_rollback_target` — `ensure_baseline()` is idempotent and
   leaves one active baseline version.
6. `test_rollback_falls_back_to_backup_when_store_has_no_target` — rollback calls
   `restore_from_backup()` and reports `rolled_back` when the store has no candidate.
7. `test_restore_from_backup_validates_and_preserves_backup` — backup restore validates a
   safe YAML change, writes the target and does not destroy the `.bak` file.

The allowlisted fixtures are now `GROQ_API_KEY` and `https://api.groq.com/openai/v1`
(`engine/intelligence/config.py:9-16,24-38`), and `validate_model_settings()` enforces the
env-name and HTTPS-host allowlists.

### Frontend asset evidence

- `frontend/dist/index.html:9` references `/assets/index-C-JosjnT.js`; the file exists and is
  **312,801 bytes**.
- `frontend/dist/index.html:10` references `/assets/index-8SWTpTb3.css`; the file exists and
  is **23,624 bytes**.
- The JS bundle contains `https://groww-algo.onrender.com` **once** and contains
  `127.0.0.1:8010` **zero times** (literal string counts). `frontend/src/api.ts:12` defaults
  to the Render base.
- Historical note §5.6 ("existence-only") is partially superseded: this re-verification
  also checked bundle contents, but did not rebuild the frontend or render it in a browser.

### Remaining limitations

The historical limitations in §5.2–§5.10 still apply except §5.1 (exception fail-open),
which is now fixed. In addition:

- Audit persistence is best-effort: `rule_engine.py:303-304` still swallows non-duplicate
  persistence exceptions. BUG-3's duplicate-key loss is fixed, but other DB write failures
  remain silent.
- The formerly stale `@unittest.expectedFailure` wording in the test module docstring was
  corrected after re-verification; this is documentation-only, not a test change.
- During this re-verification, an external process modified `README.md` and
  `docs/ai-intelligence-design.md` after the test run. Those are documentation-only changes
  not made by this reviewer; the implementation files exercised above were unchanged by
  this review.
- No browser rendering, fresh frontend build, live Groq call, real broker/ledger scan,
  deployment, commit or trading action was performed.

**Verdict:** PASS for the current working tree at the time of re-verification — 65/65
tests pass with zero expected failures and zero skips, and no test-visible defects remain.
The items above are scope limitations or non-blocking residual notes, not failed checks.

### Post-review addendum

After the independent re-verification, three error-detail strings were tightened to
type-only messages (`engine/engine.py`, `engine/intelligence/orchestrator.py`,
`engine/intelligence/agents.py`) to remove any residual chance of persisting raw
exception text. `compileall` and the full 65-test discovery suite were re-run and
passed again. No contracts, gates, config values or response shapes changed.
