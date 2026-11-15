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
│  ├─ strategy.yaml      ← every rule the bot follows (edit this, no restart)
│  └─ nifty500.txt       ← universe, one NSE symbol per line
├─ engine/
│  ├─ settings.py        config loader, IST market clock
│  ├─ groww_auth.py      TOTP token minting + on-disk token cache
│  ├─ groww_data.py      Groww quotes / candles / instrument master (read-only)
│  ├─ yahoo.py           delayed fallback feed + optional fundamentals
│  ├─ indicators.py      EMA, RSI, MACD, ATR, ADX, VWAP, Bollinger, 52w
│  ├─ patterns.py        candlestick + chart pattern detection
│  ├─ quality.py         liquidity / trend persistence / drawdown quality score
│  ├─ scoring.py         composite 0-100 score across six weighted buckets
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

1. **Scan** (every `engine.scan_interval_sec`, default 5 min, inside the entry window)
   - Bulk daily + 5-minute candles for the whole universe.
   - Indicators computed per symbol; ~20 symbols per second on cached data.
   - Patterns detected: engulfing, hammer, doji, marubozu, harami, breakout,
     breakdown, bull flag, ascending triangle, double bottom, head & shoulders,
     cup & handle. Pivot-clustered support/resistance.
   - Quality score from turnover, trend persistence, drawdown, volatility
     stability and 52-week range position. yfinance fundamentals are blended in
     when available — Groww has **no fundamentals endpoint**, so the technical
     score stands alone when they are not.
   - Composite score out of 100 across trend 22, momentum 20, pattern 20,
     volume 13, quality 15, risk 10. `QUALIFIED` ≥ 65 with ≥ 3 buckets ≥ 50 %.

2. **Entry** — only if every gate passes: qualified status, score, trend
   (price > EMA20, EMA20 > EMA50), price above VWAP but not more than 2.5 %
   extended, relative volume ≥ 1.4×, ATR 0.4–3.5 %, risk/reward ≥ 1.8.
   Quantity is risk-based: ₹100 risk per trade, capped at 45 % of equity and
   by available cash.

3. **Manage** — stop at entry − 0.6×ATR (clamped 0.4–2 % of price), target 2R,
   stop to break-even at +0.8R, then ATR trailing. Time stop 180 min.

4. **Exit** — stop, target, time stop, or hard MIS square-off at 15:20 IST.
   Fills include 0.05 % slippage per side, ₹20 brokerage, 0.025 % STT on sells.

5. **Portfolio gates** — max 2 open positions, ₹200 daily loss cap, ₹10,000
   starting capital.

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

The page polls every 15 s (trades every 30 s), so it updates on its own.

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
