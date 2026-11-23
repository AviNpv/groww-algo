# Live agent group chat

The **Agent Chat** tab turns one symbol's advisory review into a real-time
group conversation. Every bubble is either a real agent report or a real
deterministic check — nothing in the live view is pre-scripted.

The funny, fully scripted version remains available at `/agent-demo.html` for
comparison. It never calls a model.

## Safety invariants

- **Paper-only.** The chat never imports a broker, never calls an order
  function and never places a trade. Its final `decision` event is advisory.
- **Read-only.** It does not write strategy YAML, does not promote a strategy
  and does not change config. The only writes are the normal intelligence
  evidence rows (`trade_decision`, execution log, debate, reports).
- **Fail-closed.** Agent errors stay errors and are shown in the chat. The
  deterministic debate and rule engine still decide the outcome.
- **One run at a time.** A second start while a run is active returns HTTP 409;
  a short cooldown (20 s) limits repeated starts.
- **AI mode required.** If `intelligence.enabled` is false, start returns HTTP
  409 with a clear message.
- **No order capability.** The route set is asserted by
  `test_no_order_endpoint_in_intelligence_router`.
- **No secret leakage.** Every SSE string is capped and passed through the same
  redaction rules as the other intelligence responses.

## Flow

```
one-symbol scan
  -> market snapshot
  -> six agent reports (market regime, scanner, technical, strategy, critic, risk)
  -> deterministic debate
  -> deterministic rules
  -> fail-closed decision
```

This is the same pipeline the engine uses before a paper entry, with an event
hook added for observability.

## API

### `POST /api/intelligence/chat/start`

```json
{ "symbol": "UNITDSPR" }
```

Returns:

```json
{
  "ok": true,
  "session_id": "chat-1a2b3c4d5e6f",
  "symbol": "UNITDSPR",
  "paper_only": true,
  "stream": "/api/intelligence/chat/chat-1a2b3c4d5e6f/stream",
  "started_at": "2026-11-23T14:05:00+05:30"
}
```

Errors: `400` invalid symbol, `409` AI off or a run is already active,
`429` cooldown, `503` intelligence config unavailable.

### `GET /api/intelligence/chat/{session_id}/stream`

Server-Sent Events. Each frame has an `id` and a JSON `data` payload. The
server sends `: keep-alive` comments while an agent is still thinking, so a
long free-tier model call does not look like a dead connection. Reconnects
resume from `Last-Event-ID` (or `?after=`).

Event types:

| type | meaning |
| --- | --- |
| `session` | run accepted; includes `paper_only` |
| `status` | progress note |
| `warning` | recoverable issue (for example a live scan fallback) |
| `scan` | one-symbol scan summary |
| `snapshot` | market session, plan levels, feed freshness |
| `agent_start` | one agent began its model call (typing indicator) |
| `agent_report` | real report: summary, recommendation, confidence, status |
| `debate` | deterministic verdict, consensus, support/oppose/abstain |
| `rules` | deterministic checks and reasons |
| `decision` | final `ALLOW`/`REJECT`, `model_ok`, rationale, `paper_only` |
| `error` | terminal or per-step error |
| `done` | terminal frame with `ok` |

### `GET /api/intelligence/chat/status`

Process-local session list for debugging. Contains no secrets.

## UI

Open the dashboard and choose **Agent Chat**. Pick a symbol from the latest
scan (or type one) and press **Start live debate**. Agent bubbles appear as
each model call returns; the final decision is highlighted.

For the cleanest run, stop the engine first. The engine's automatic scan cycles
also use the model, and the free tier can rate-limit a shared quota. A
rate-limited agent is shown as an error and the decision fails closed — that is
the intended behaviour, not a silent pass.

## Tests

`tests/test_agent_chat.py` covers:

- orchestrator event order and legacy runners without the hook;
- broken listeners cannot change a decision;
- runtime agent event emission;
- pipeline event sequence and no-data handling;
- session limits, symbol validation, secret redaction and SSE framing;
- the registered route list.

Run:

```
.\.venv\Scripts\python.exe -m unittest tests.test_agent_chat -v
```
