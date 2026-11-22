# Groww Algo — intraday scanner + automatic paper trader

A self-hosted intraday desk for NSE equities. It scans the whole universe,
scores every stock with a multi-factor model, and automatically paper-trades
the best setups under fixed risk rules. The dashboard is a dark, Binance-style
web app with a **P&L calendar**.

> **Paper only.** This build has no order endpoint. It cannot send a real order
> to Groww or any other broker, even with valid credentials. Everything is
> simulated against a local SQLite ledger.

---

## Quick start (Windows)

```bat
cd C:\Users\essve\groww-algo
run_all.bat
```

That opens two consoles and your browser:

| Service | URL |
|---|---|
| Dashboard | http://127.0.0.1:5173 |
| API | http://127.0.0.1:8000 |
| API docs | http://127.0.0.1:8000/docs |

Run them separately with `run_api.bat` and `run_web.bat` if you prefer.

### First-time setup by hand

```bat
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
.\.venv\Scripts\python.exe -m scripts.seed_demo      :: optional demo calendar
.\.venv\Scripts\python.exe -m uvicorn engine.api:app --port 8000
```

Frontend (Node 20+):

```bat
cd frontend
npm install
npm run dev
```

Production single-port mode: `cd frontend && npm run build`, then the API
serves `frontend/dist` at http://127.0.0.1:8000.

---

## Layout

```
groww-algo/
├─ config/
│  ├─ strategy.yaml      ← V4 paper-trading rules (edit this, no restart)
│  ├─ strategy.default.yaml ← shipped V4 defaults for "Restore"
│  ├─ nifty500_symbols.txt ← V4 universe, one NSE symbol per line
│  ├─ nifty500.txt       ← legacy universe file
│  └─ nse_holidays.txt   ← exchange holidays for the intelligence scheduler
├─ engine/
│  ├─ settings.py        config loader, IST market clock
│  ├─ groww_auth.py      TOTP token minting + on-disk token cache
│  ├─ groww_data.py      Groww quotes / candles / instrument master (read-only)
│  ├─ yahoo.py           delayed fallback feed + optional fundamentals
│  ├─ indicators.py      EMA, RSI, MACD, ATR, ADX, VWAP, Bollinger, 52w
│  ├─ patterns.py        candlestick + chart pattern detection
│  ├─ quality.py         liquidity / trend persistence / drawdown quality score
│  ├─ scoring.py         V4 0-100 score across seven weighted categories
│  ├─ universe.py        symbol loading + price/turnover filters
│  ├─ marketdata.py      unified feed: Groww when authorised, else Yahoo
│  ├─ scanner.py         full-universe scan pipeline
│  ├─ strategy.py        entry filters, position sizing, exit management
│  ├─ broker.py          simulated fills with slippage + brokerage + STT
│  ├─ ledger.py          SQLite: account, trades, positions, daily equity
│  ├─ engine.py          the automation loop (scan → enter → manage → square off)
│  ├─ api.py             FastAPI surface used by the dashboard
│  └─ intelligence/      opt-in advisory layer (separate store, no orders)
│     ├─ models.py       frozen evidence/debate/decision contracts
│     ├─ config.py       safe config access + credential endpoint allowlist
│     ├─ storage.py      data/intelligence.sqlite evidence store
│     ├─ snapshot.py     validated timestamped market snapshot
│     ├─ model_adapter.py bounded OpenAI-compatible / local model calls
│     ├─ prompts.py      six agent prompts + structured schemas
│     ├─ agents.py       six advisory agents
│     ├─ debate.py       deterministic multi-agent debate
│     ├─ rule_engine.py  independent risk/session/rule gate
│     ├─ orchestrator.py single gate before the paper buy
│     ├─ evolution.py    research, validation, promotion, rollback
│     ├─ scheduler.py    24×7 research / session-only scanning
│     └─ api.py          additive /api/intelligence/* router
├─ scripts/seed_demo.py  labelled DEMO history for the calendar
├─ frontend/             React 19 + Vite + TypeScript dashboard
│  └─ src/components/IntelligencePanel.tsx ← new evidence tab
├─ docs/ai-*.md          design, QA and security evidence
└─ data/
   ├─ algo.sqlite        runtime ledger state (git-ignored)
   └─ intelligence.sqlite separate advisory evidence (git-ignored)
```

---

## How a trade happens

1. **Scan** (every `engine.scan_interval_seconds`, 60 s in V4, inside 09:25–14:45)
   - Bulk daily + 5-minute closed candles; 15-minute trend confirmation.
   - Indicators computed per symbol; ~20 symbols per second on cached data.
   - Patterns detected: engulfing, hammer, doji, marubozu, harami, breakout,
     breakdown, bull flag, ascending triangle, double bottom, head & shoulders,
     cup & handle. Pivot-clustered support/resistance.
   - Quality score from turnover, trend persistence, drawdown, volatility
     stability and 52-week range position. yfinance fundamentals are blended in
     when available — Groww has **no fundamentals endpoint**, so the technical
     score stands alone when they are not.
   - V4 100-point score: trend 22, momentum 16, price pattern 16, volume 14,
     market/sector 12, stock quality 10, risk quality 10. `QUALIFIED` needs 85+
     with all 5 factor confirmations ≥ 50 %.

2. **Entry** — long gates: qualified status, score ≥ 85, 5 confirmations,
   EMA20 > EMA50 > EMA200, price above VWAP, RSI 55–72, ADX ≥ 20, relative
   volume ≥ 1.5×, ATR 0.4–3.5 %, R:R ≥ 2, closed candle, and fresh Groww
   confirmation. Yahoo is scanning-only in V4.

3. **Manage** — stop at entry − 1.2×ATR (clamped 0.4–2 %), target 2R, stop to
   break-even at +1R, then stepped ATR + profit-lock trailing. Time stop 180 min.

4. **Exit** — stop, target, time stop, or hard square-off of all positions at
   15:20 IST. Fills include 0.05 % slippage, V4 brokerage (0.10 %, ₹20 cap),
   exchange/SEBI/GST/STT charges.

5. **Portfolio gates** — max 2 positions, 4 trades/day, 1 trade/symbol/day,
   ₹300 daily loss cap, 15-min loss cooldown, 3-loss halt, ₹10,000 starting capital.

6. **Intraday leverage** — Groww-style 5× buying power (`account.intraday_leverage`).
   Cash is blocked as `order value / 5`, while exposure and cash caps use the
   leveraged amount. The ₹100 risk-per-trade and ₹300 daily-loss caps follow the
   configured 1% / 3% of capital, so a stop still costs ₹100 — leverage only
   removes the small-capital ceiling.

All of that is in `config/strategy.yaml`. Nothing is hard-coded in the loop.

---

## Data sources

| Feed | When used | Latency |
|---|---|---|
| Groww API | `GROWW_TOTP_TOKEN` + `GROWW_TOTP_SECRET` present and authorised | real-time |
| Yahoo Finance | fallback, or while Groww auth is missing | ~15 min delayed |

The header pill shows `GROWW LIVE` or `YAHOO DELAYED`, so you always know which
one is feeding the bot. Candle history always comes from Yahoo bulk download for
speed, with per-symbol Groww candles as a fallback.

### Enabling Groww

1. Create an API key at <https://groww.in/trade-api/api-keys>.
2. Copy `.env.example` to `.env`.
3. Set `GROWW_TOTP_TOKEN` to the API key.
4. Set `GROWW_TOTP_SECRET` to the base32 secret behind the authenticator entry.
5. Restart the API. The access token is cached in
   `data/cache/groww_access_token.json` and reused until it expires
   (150 token mints/day limit).

---

## The dashboard

- **Overview** — equity, today's P&L, total P&L, win rate, profit factor, max
  drawdown; the **P&L calendar** (click any day for session detail); equity
  curve; bot status and controls.
- **Scanner** — ranks the universe with score, status, per-factor bars, pattern
  chips, relative volume, ATR. Click a row for the full breakdown: every reason
  the model liked it, support/resistance/VWAP, all indicators, quality
  components, and the indicative stop/target.
- **Positions** — live paper positions with unrealised P&L, R multiple, stop and
  target, and a manual close button.
- **Trades** — the full ledger with charges and exit reasons.
- **Intelligence** — opt-in agent evidence, debate, model usage, rule decisions,
  strategy versions and the operator-token controls. It has its own 5 s read
  poll and does **not** change the 1 s `/api/live` loop.
- **Settings** — runtime config and the raw `strategy.yaml`.

The page polls `/api/live` every 1 s, so the dashboard and calendar stay in sync.

### Runtime intelligence layer (opt-in)

The six-agent layer is additive and **off by default**. Leave
`intelligence.enabled: false` in `config/strategy.yaml` and the original
deterministic scanner/broker path is unchanged. When enabled:

1. A validated, timestamped snapshot is built from the real scanner, feed health,
   ledger and config. Missing information is explicit.
2. Six advisory agents report evidence, uncertainty and objections:
   Market Regime Analyst, Stock Scanner, Technical Analyst, Strategy Analyst,
   Adversarial Critic and Risk Analyst. The Critic and Risk Analyst are mandatory.
3. A deterministic debate rejects any candidate with an unresolved critical
   objection. It never asks a model to arbitrate.
4. A separate deterministic rule engine re-checks session, freshness, score ≥85,
   R:R ≥2, stop/target, sizing, exposure, daily loss and duplicates. Agents
   cannot bypass it.
5. A single gate runs before `broker.buy`. If the model, debate, rule engine or
   orchestration raises, a **new entry is rejected**. Existing positions are
   still managed by the original deterministic exit chain.
6. All evidence is written to `data/intelligence.sqlite`, never to the trading
   ledger. There is no order endpoint in the intelligence router.

#### Model setup

The default provider is a free OpenAI-compatible endpoint:

```yaml
intelligence:
  enabled: false
  require_for_entries: true
  fail_closed: true
  model:
    provider: openai_compatible
    base_url: https://api.groq.com/openai/v1
    model: llama-3.3-70b-versatile
    api_key_env: GROQ_API_KEY
    allow_paid: false
```

Set `GROQ_API_KEY` in the backend environment (or use the allowlisted
`INTELLIGENCE_MODEL_API_KEY`). A localhost OpenAI-compatible server may be
configured through `local_endpoint` with host `127.0.0.1`, `localhost` or `::1`;
localhost calls never receive a hosted credential. Allowed hosted endpoints are
`api.groq.com` and `generativelanguage.googleapis.com`. Unknown environment
variable names or unapproved hosts are rejected by both the adapter and the
Settings YAML validator. `allow_paid` is forced to `false` at runtime. There is
no silent fallback to another provider or a paid model.

Until a key/endpoint is configured, `/api/intelligence/health` reports
`DEGRADED` and AI-dependent entries fail closed. No key value is ever stored in
YAML, prompts, logs, evidence rows or API responses.

#### Strategy-changing actions

Research, promote and rollback are protected by an explicit operator token:

```bat
set INTELLIGENCE_ADMIN_TOKEN=choose-a-long-random-local-secret
```

Restart the backend after setting it. The Intelligence tab has an
**Operator token** field; it sends `X-Intelligence-Token` and keeps the value in
`sessionStorage` only. When the token is unset, those three POST endpoints
return HTTP 503 and mutation is disabled. The token is never accepted in a query
string. Read-only intelligence endpoints need no token.

Strategy evolution is deterministic code, not an LLM action. It searches only
four numeric strategy leaves, validates with the existing backtester and
walk-forward/out-of-sample splits, requires real shadow-session evidence before
promotion (`auto_promote: false` by default), seeds a baseline before the first
promotion, keeps the previous version, and can roll back from either the version
store or `strategy.yaml.bak`.

---

## Resetting before a real paper run

The seeded data is marked DEMO and shows a banner. To start clean:

```bat
.\.venv\Scripts\python.exe -c "from engine import ledger; ledger.reset(demo=False); print('ledger reset')"
```

The bot keeps its own history from then on and the calendar fills with actual
paper sessions.

---

## What is deliberately missing

- **No live order placement.** Order endpoints are absent from the code by
  design. Real orders would additionally require Groww API-key approval, a
  static-IP whitelist, and SEBI algo registration/tagging through the broker.
- **No fundamentals from Groww.** The API exposes orders, holdings, positions,
  margin, quotes, candles and option chain — nothing fundamental. Quality uses
  price/volume behaviour plus optional yfinance data.
- **No live model by default.** The intelligence layer is off and has no key,
  so AI-dependent entries are rejected rather than guessed.
- **No live Groww market-data approval yet.** V4 entries require a fresh Groww
  quote; Yahoo remains scanning-only and delayed until that approval exists.

## Honest limitations

- Chart-pattern detection is heuristic. It produces false positives by nature;
  treat patterns as one factor among six, not as a signal on its own.
- `short_entry` is configured but the current runtime implements LONG entries
  only. The intelligence layer does not silently enable shorts.
- Backtests are used only to vet strategy-evolution candidates; the calendar
  remains forward paper results. Walk-forward splits are an approximation of a
  true point-in-time pipeline and are documented in `docs/ai-testing-report.md`.
- No live hosted-model or full end-to-end trade smoke was performed. Model tests
  use mocked transports; independent QA verified contracts and gates, not
  model quality. Paper results and simulated returns are not performance
  guarantees or investment advice.
- Yahoo's NSE data is delayed and occasionally has gaps or splits that need
  cleaning. Groww real-time removes the delay but not the data quality caveats.
- A ₹10,000 account with ₹100 risk per trade produces very small position
  sizes on higher-priced stocks. Some qualified setups will be skipped because
  one share exceeds the exposure cap — that is the sizing rule working, not a bug.
- Simulated results say nothing about future returns. This is research tooling,
  not investment advice.

---

## Validation evidence (local checkpoint, not deployed)

```bat
.\.venv\Scripts\python.exe -m compileall -q engine tests scripts
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
cd frontend && npm run build
```

Latest local run: **65 tests passed, 0 failed, 0 expected failures, 0 skipped**.
The production bundle is committed under `frontend/dist/` with
`index-C-JosjnT.js` and `index-8SWTpTb3.css`; `dist/index.html` references those
files and the bundle targets `https://groww-algo.onrender.com` (no local port).

Final local HTTP smoke (127.0.0.1:8010, with a throwaway admin token) returned
200 for `/api/health`, every read-only `/api/intelligence/*` endpoint checked,
and `/api/live`. Overview and calendar today-P&L both reported `0.00` with
delta `0.00`. All three strategy-changing POST routes returned 401 with no
token or a wrong token; with the valid token, promote/rollback reached route
logic (404 for a missing version, 400 for an invalid version ID) without
changing `strategy.yaml`. The authenticated `evolution/run` call was not
awaited because the local research job runs multi-symbol backtests; its admin
gate was verified by the 401 cases and code review.

Independent evidence:

- `docs/ai-testing-report.md` — QA suite, coverage matrix, defects and limits.
- `docs/ai-security-audit.md` — paper-only boundary, credential/token controls,
  final security verdict.
- `docs/ai-intelligence-design.md` — architecture and phase gates.

Nothing on this branch has been pushed or deployed. Production deployment
requires QA/security sign-off and explicit approval. Keep
`require_for_entries: true`, `fail_closed: true`, `auto_promote: false` and
`allow_paid: false`.
