"""Populate the ledger with clearly-labelled DEMO history so the calendar and
charts are not empty before the first live paper session.

Run:  .venv\\Scripts\\python.exe -m scripts.seed_demo
"""
from __future__ import annotations

import datetime as dt
import random

from engine import ledger
from engine.settings import cfg, now_ist

SYMBOLS = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN",
           "BHARTIARTL", "ITC", "LT", "AXISBANK", "TITAN", "SUNPHARMA",
           "MARUTI", "BAJFINANCE", "ASIANPAINT", "KOTAKBANK", "M&M",
           "TATAMOTORS", "ADANIENT", "JSWSTEEL"]

EXIT_REASONS = ["TARGET", "TARGET", "TARGET", "STOP_LOSS", "TRAILING_STOP", "TIME_STOP"]


def trading_days(count: int) -> list[dt.date]:
    day = now_ist().date() - dt.timedelta(days=1)
    out: list[dt.date] = []
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day -= dt.timedelta(days=1)
    return sorted(out)


def main(count: int = 45, seed: int = 42) -> None:
    random.seed(seed)
    capital = float(cfg("account", "starting_capital", default=10000))
    ledger.reset(starting_capital=capital, demo=True)

    equity = capital
    realized_total = 0.0
    rows_equity = []
    rows_trades = []

    for day in trading_days(count):
        day_pnl = random.gauss(28, 115)
        day_pnl = max(-260, min(320, day_pnl))
        n_trades = random.choices([0, 1, 2, 3], weights=[12, 38, 38, 12])[0]
        if n_trades == 0:
            day_pnl = 0.0

        wins = losses = 0
        weights = [random.uniform(0.6, 1.6) for _ in range(n_trades)] if n_trades else []
        total_w = sum(weights) or 1.0
        for i in range(n_trades):
            symbol = random.choice(SYMBOLS)
            qty = random.choice([5, 8, 10, 12, 15, 20, 25, 30, 40])
            entry = round(random.uniform(120, 3200), 2)
            charges = 40.0
            pnl = round(day_pnl * weights[i] / total_w, 2)
            exit_price = round(entry + (pnl + charges) / qty, 2)
            reason = "TARGET" if pnl > 0 else random.choice(["STOP_LOSS", "TRAILING_STOP", "TIME_STOP"])
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
            open_ts = dt.datetime.combine(day, dt.time(9, 30)) + dt.timedelta(minutes=random.randint(0, 180))
            close_ts = open_ts + dt.timedelta(minutes=random.randint(20, 240))
            score = round(random.uniform(66, 92), 1)
            rows_trades.append((
                symbol, qty, entry, exit_price, entry * 0.988, entry * 1.024,
                open_ts.isoformat(timespec="seconds"), close_ts.isoformat(timespec="seconds"),
                pnl, charges, score, reason, "demo replay", "paper", 1,
            ))

        equity = round(equity + day_pnl, 2)
        realized_total += day_pnl
        rows_equity.append((day.isoformat(), equity, equity, 0.0, round(day_pnl, 2),
                            0.0, n_trades, wins, losses, "DEMO DATA", 1))

    with ledger._conn() as conn:
        conn.executemany(
            "INSERT INTO trades (symbol, qty, entry_price, exit_price, stop, target,"
            " opened_at, closed_at, pnl, charges, score, exit_reason, reason, mode, demo)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows_trades,
        )
        conn.executemany(
            "INSERT INTO equity_daily (day, equity, cash, market_value, realized_pnl,"
            " unrealized_pnl, trades, wins, losses, notes, demo)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows_equity,
        )
        conn.execute(
            "UPDATE account SET cash = ?, realized_pnl = ?, day_date = ?,"
            " day_start_equity = ?, demo = 1 WHERE id = 1",
            (equity, round(realized_total, 2), now_ist().date().isoformat(), equity),
        )

    print(f"Seeded {len(rows_equity)} demo sessions and {len(rows_trades)} demo trades.")
    print(f"Start {capital:,.2f} -> End {equity:,.2f} ({equity / capital - 1:+.2%}), DEMO data.")
    print("The dashboard shows a DEMO banner until you reset for live paper trading.")


if __name__ == "__main__":
    main()
