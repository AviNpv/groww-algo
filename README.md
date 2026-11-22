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
│  └─ nifty500.txt       ← legacy universe file
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
│  └─ api.py             FastAPI surface used by the dashboard
├─ scripts/seed_demo.py  labelled DEMO history for the calendar
├─ frontend/             React 19 + Vite + TypeScript dashboard
└─ data/algo.sqlite      runtime state (git-ignored)
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
- **Settings** — runtime config and the raw `strategy.yaml`.

The page polls `/api/live` every 1 s, so the dashboard and calendar stay in sync.

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
- **No backtester.** The calendar is forward paper results, not a backtest.
  Backtesting this scorer properly is a separate job.

## Honest limitations

- Chart-pattern detection is heuristic. It produces false positives by nature;
  treat patterns as one factor among six, not as a signal on its own.
- Yahoo's NSE data is delayed and occasionally has gaps or splits that need
  cleaning. Groww real-time removes the delay but not the data quality caveats.
- A ₹10,000 account with ₹100 risk per trade produces very small position
  sizes on higher-priced stocks. Some qualified setups will be skipped because
  one share exceeds the exposure cap — that is the sizing rule working, not a bug.
- Simulated results say nothing about future returns. This is research tooling,
  not investment advice.
