"""SQLite ledger: account, positions, closed trades, daily equity, signals, scans.

One file: data/algo.sqlite. WAL mode + short-lived connections so the API and
the engine thread can share it safely.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from contextlib import contextmanager

from engine.settings import DB_PATH, cfg, now_ist

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash REAL NOT NULL,
    starting_capital REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    day_date TEXT,
    day_start_equity REAL,
    demo INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    qty INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    stop REAL,
    target REAL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    pnl REAL,
    charges REAL NOT NULL DEFAULT 0,
    score REAL,
    exit_reason TEXT,
    reason TEXT,
    mode TEXT NOT NULL DEFAULT 'paper',
    demo INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS positions (
    trade_id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    qty INTEGER NOT NULL,
    avg_price REAL NOT NULL,
    stop REAL,
    target REAL,
    initial_risk REAL,
    highest_price REAL,
    last_price REAL,
    opened_at TEXT NOT NULL,
    score REAL,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS equity_daily (
    day TEXT PRIMARY KEY,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    market_value REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    unrealized_pnl REAL NOT NULL DEFAULT 0,
    trades INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    demo INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    symbol TEXT NOT NULL,
    score REAL,
    status TEXT,
    price REAL,
    stop REAL,
    target REAL,
    qty INTEGER,
    action TEXT,
    factors TEXT,
    patterns TEXT,
    demo INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    duration_ms INTEGER,
    scanned INTEGER,
    qualified INTEGER,
    watchlist INTEGER,
    source TEXT,
    error TEXT,
    demo INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_signals_day ON signals(day);
"""


@contextmanager
def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(starting_capital: float | None = None) -> None:
    cap = float(starting_capital or cfg("account", "starting_capital", default=10000))
    with _lock, _conn() as conn:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT id FROM account WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO account (id, cash, starting_capital, realized_pnl, day_date, day_start_equity)"
                " VALUES (1, ?, ?, 0, ?, ?)",
                (cap, cap, now_ist().date().isoformat(), cap),
            )


def reset(starting_capital: float | None = None, demo: bool = True) -> None:
    cap = float(starting_capital or cfg("account", "starting_capital", default=10000))
    with _lock, _conn() as conn:
        conn.executescript(SCHEMA)
        conn.execute("DELETE FROM positions")
        conn.execute("DELETE FROM trades")
        conn.execute("DELETE FROM signals")
        conn.execute("DELETE FROM scan_runs")
        conn.execute("DELETE FROM equity_daily")
        conn.execute("DELETE FROM account")
        conn.execute(
            "INSERT INTO account (id, cash, starting_capital, realized_pnl, day_date, day_start_equity, demo)"
            " VALUES (1, ?, ?, 0, ?, ?, ?)",
            (cap, cap, now_ist().date().isoformat(), cap, 1 if demo else 0),
        )


def is_demo() -> bool:
    with _conn() as conn:
        row = conn.execute("SELECT demo FROM account WHERE id = 1").fetchone()
        return bool(row["demo"]) if row else False


def set_demo(value: bool) -> None:
    with _lock, _conn() as conn:
        conn.execute("UPDATE account SET demo = ? WHERE id = 1", (1 if value else 0,))


# --------------------------------------------------------------------------
# account / day rollover
# --------------------------------------------------------------------------
def account() -> dict:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
    return dict(row) if row else {}


def _market_value_and_unrealized(conn) -> tuple[float, float]:
    rows = conn.execute("SELECT * FROM positions").fetchall()
    market_value = 0.0
    unrealized = 0.0
    for row in rows:
        last = float(row["last_price"] or row["avg_price"])
        market_value += last * int(row["qty"])
        unrealized += (last - float(row["avg_price"])) * int(row["qty"])
    return market_value, unrealized


def current_equity() -> float:
    with _conn() as conn:
        acc = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        mv, _ = _market_value_and_unrealized(conn)
    return round(float(acc["cash"]) + mv, 2) if acc else 0.0


def ensure_day() -> dict:
    """Roll the daily baseline forward when the calendar day changes."""
    today = now_ist().date().isoformat()
    with _lock, _conn() as conn:
        acc = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        if acc is None:
            init_db()
            acc = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        if acc["day_date"] != today:
            equity = float(acc["cash"]) + _market_value_and_unrealized(conn)[0]
            prev_day = acc["day_date"]
            if prev_day:
                _snapshot_conn(conn, prev_day)
            conn.execute(
                "UPDATE account SET day_date = ?, day_start_equity = ? WHERE id = 1",
                (today, equity),
            )
    return account()


# --------------------------------------------------------------------------
# trades / positions
# --------------------------------------------------------------------------
def set_cash(cash: float, realized: float | None = None) -> None:
    with _lock, _conn() as conn:
        if realized is None:
            conn.execute("UPDATE account SET cash = ? WHERE id = 1", (float(cash),))
        else:
            conn.execute("UPDATE account SET cash = ?, realized_pnl = ? WHERE id = 1",
                         (float(cash), float(realized)))


def adjust_cash(delta: float) -> None:
    with _lock, _conn() as conn:
        conn.execute("UPDATE account SET cash = cash + ? WHERE id = 1", (float(delta),))


def open_trade(symbol: str, qty: int, price: float, stop: float, target: float,
               score: float, reason: str, charges: float = 0.0,
               mode: str = "paper", demo: bool | None = None,
               ts: str | None = None) -> int:
    ts = ts or now_ist().isoformat(timespec="seconds")
    demo_flag = 1 if (is_demo() if demo is None else demo) else 0
    with _lock, _conn() as conn:
        cur = conn.execute(
            "INSERT INTO trades (symbol, qty, entry_price, stop, target, opened_at,"
            " charges, score, reason, mode, demo) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (symbol, int(qty), float(price), float(stop), float(target), ts,
             float(charges), float(score), reason, mode, demo_flag),
        )
        trade_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO positions (trade_id, symbol, qty, avg_price, stop, target,"
            " initial_risk, highest_price, last_price, opened_at, score, reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, symbol, int(qty), float(price), float(stop), float(target),
             float(price) - float(stop), float(price), float(price), ts, float(score), reason),
        )
    return trade_id


def update_position(trade_id: int, stop: float | None = None,
                    highest: float | None = None, last: float | None = None) -> None:
    sets, values = [], []
    if stop is not None:
        sets.append("stop = ?")
        values.append(float(stop))
    if highest is not None:
        sets.append("highest_price = ?")
        values.append(float(highest))
    if last is not None:
        sets.append("last_price = ?")
        values.append(float(last))
    if not sets:
        return
    values.append(int(trade_id))
    with _lock, _conn() as conn:
        conn.execute(f"UPDATE positions SET {', '.join(sets)} WHERE trade_id = ?", values)


def close_trade(trade_id: int, price: float, charges: float, reason: str,
                ts: str | None = None) -> dict | None:
    ts = ts or now_ist().isoformat(timespec="seconds")
    with _lock, _conn() as conn:
        pos = conn.execute("SELECT * FROM positions WHERE trade_id = ?", (trade_id,)).fetchone()
        if pos is None:
            return None
        trade = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        qty = int(pos["qty"])
        entry = float(pos["avg_price"])
        gross = (float(price) - entry) * qty
        total_charges = float(trade["charges"] or 0) + float(charges or 0)
        pnl = gross - total_charges
        conn.execute(
            "UPDATE trades SET exit_price = ?, closed_at = ?, pnl = ?, charges = ?,"
            " exit_reason = ? WHERE id = ?",
            (float(price), ts, pnl, total_charges, reason, int(trade_id)),
        )
        conn.execute("DELETE FROM positions WHERE trade_id = ?", (int(trade_id),))
        acc = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        cash = float(acc["cash"]) + float(price) * qty
        realized = float(acc["realized_pnl"]) + pnl
        conn.execute("UPDATE account SET cash = ?, realized_pnl = ? WHERE id = 1", (cash, realized))
    return {"pnl": round(pnl, 2), "gross": round(gross, 2), "charges": round(total_charges, 2)}


def positions() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM positions ORDER BY opened_at").fetchall()
    out = []
    for row in rows:
        p = dict(row)
        last = float(p["last_price"] or p["avg_price"])
        p["last_price"] = round(last, 2)
        p["value"] = round(last * int(p["qty"]), 2)
        p["unrealized"] = round((last - float(p["avg_price"])) * int(p["qty"]), 2)
        p["unrealized_pct"] = round((last / float(p["avg_price"]) - 1) * 100, 2) if p["avg_price"] else 0.0
        p["r_multiple"] = round((last - float(p["avg_price"])) / float(p["initial_risk"]), 2) \
            if p.get("initial_risk") else None
        out.append(p)
    return out


def trades(limit: int = 200, symbol: str | None = None) -> list[dict]:
    query = "SELECT * FROM trades"
    params: list = []
    if symbol:
        query += " WHERE symbol = ?"
        params.append(symbol.upper())
    query += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with _conn() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


# --------------------------------------------------------------------------
# equity snapshots / calendar
# --------------------------------------------------------------------------
def _snapshot_conn(conn, day: str) -> None:
    acc = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
    if acc is None:
        return
    mv, unrealized = _market_value_and_unrealized(conn)
    cash = float(acc["cash"])
    equity = cash + mv
    closed = conn.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) w,"
        " SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) l, SUM(pnl) p"
        " FROM trades WHERE substr(closed_at, 1, 10) = ?", (day,)
    ).fetchone()
    realized = float(closed["p"] or 0.0)
    conn.execute(
        "INSERT INTO equity_daily (day, equity, cash, market_value, realized_pnl,"
        " unrealized_pnl, trades, wins, losses, demo) VALUES (?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(day) DO UPDATE SET equity=excluded.equity, cash=excluded.cash,"
        " market_value=excluded.market_value, realized_pnl=excluded.realized_pnl,"
        " unrealized_pnl=excluded.unrealized_pnl, trades=excluded.trades,"
        " wins=excluded.wins, losses=excluded.losses",
        (day, equity, cash, mv, realized, unrealized, int(closed["n"] or 0),
         int(closed["w"] or 0), int(closed["l"] or 0), int(acc["demo"] or 0)),
    )


def snapshot_equity(day: str | None = None) -> None:
    day = day or now_ist().date().isoformat()
    with _lock, _conn() as conn:
        _snapshot_conn(conn, day)


def equity_curve(limit: int = 400) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM equity_daily ORDER BY day DESC LIMIT ?", (int(limit),)
        ).fetchall()
    return sorted([dict(r) for r in rows], key=lambda r: r["day"])


def calendar_month(year: int, month: int) -> list[dict]:
    prefix = f"{year:04d}-{month:02d}"
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM equity_daily WHERE day LIKE ? ORDER BY day", (prefix + "%",)
        ).fetchall()
        days = {r["day"]: dict(r) for r in rows}
    out = []
    previous = None
    for day in sorted(days):
        row = days[day]
        if previous is not None:
            row["pnl"] = round(float(row["equity"]) - float(previous["equity"]), 2)
            row["pnl_pct"] = round((float(row["equity"]) / float(previous["equity"]) - 1) * 100, 2) \
                if previous["equity"] else 0.0
        else:
            # First session in the month: compare against the prior session's
            # closing equity so month boundaries do not hide a day's P&L.
            with _conn() as conn:
                prior = conn.execute(
                    "SELECT equity FROM equity_daily WHERE day < ? ORDER BY day DESC LIMIT 1",
                    (day,),
                ).fetchone()
                acc = conn.execute("SELECT starting_capital FROM account WHERE id = 1").fetchone()
            base = float(prior["equity"]) if prior else float((acc or {})["starting_capital"] or row["equity"] or 1)
            row["pnl"] = round(float(row["equity"]) - base, 2)
            row["pnl_pct"] = round(row["pnl"] / base * 100, 2) if base else 0.0
        previous = row
        out.append(row)
    return out


# --------------------------------------------------------------------------
# signals / scans
# --------------------------------------------------------------------------
def record_signals(items: list[dict], demo: bool | None = None) -> None:
    if not items:
        return
    ts = now_ist().isoformat(timespec="seconds")
    day = now_ist().date().isoformat()
    demo_flag = 1 if (is_demo() if demo is None else demo) else 0
    with _lock, _conn() as conn:
        conn.executemany(
            "INSERT INTO signals (ts, day, symbol, score, status, price, stop, target,"
            " qty, action, factors, patterns, demo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(ts, day, i["symbol"], i.get("score"), i.get("status"), i.get("price"),
              i.get("stop"), i.get("target"), i.get("qty"), i.get("action"),
              json.dumps(i.get("buckets", {})), json.dumps(i.get("patterns", [])), demo_flag)
             for i in items],
        )


def record_scan(duration_ms: int, scanned: int, qualified: int, watchlist: int,
                source: str, error: str | None = None) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO scan_runs (ts, duration_ms, scanned, qualified, watchlist,"
            " source, error, demo) VALUES (?,?,?,?,?,?,?,?)",
            (now_ist().isoformat(timespec="seconds"), int(duration_ms), int(scanned),
             int(qualified), int(watchlist), source, error,
             1 if is_demo() else 0),
        )


def latest_scan() -> dict:
    with _conn() as conn:
        run = conn.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
        if run is None:
            return {"run": None, "signals": []}
        signals = conn.execute(
            "SELECT * FROM signals WHERE ts = ? ORDER BY score DESC", (run["ts"],)
        ).fetchall()
    return {
        "run": dict(run),
        "signals": [{**dict(s),
                     "factors": json.loads(s["factors"] or "{}"),
                     "patterns": json.loads(s["patterns"] or "[]")} for s in signals],
    }


def signals_for_day(day: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM signals WHERE day = ? ORDER BY score DESC", (day,)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# overview metrics
# --------------------------------------------------------------------------
def overview() -> dict:
    with _conn() as conn:
        acc = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        if acc is None:
            init_db()
            acc = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        mv, unrealized = _market_value_and_unrealized(conn)
        closed = conn.execute("SELECT * FROM trades WHERE closed_at IS NOT NULL").fetchall()
        day_rows = conn.execute("SELECT * FROM equity_daily ORDER BY day").fetchall()
        open_count = conn.execute("SELECT COUNT(*) n FROM positions").fetchone()["n"]

    cash = float(acc["cash"])
    equity = cash + mv
    start = float(acc["starting_capital"]) or 1.0
    realized = float(acc["realized_pnl"])
    pnls = [float(t["pnl"] or 0) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    equities = [float(r["equity"]) for r in day_rows] or [equity]
    peak = equities[0]
    max_dd = 0.0
    for value in equities:
        peak = max(peak, value)
        if peak:
            max_dd = min(max_dd, (value / peak - 1) * 100)
    day_changes = [float(day_rows[i]["equity"]) - float(day_rows[i - 1]["equity"])
                   for i in range(1, len(day_rows))]
    day_start = float(acc["day_start_equity"] or start)

    return {
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "market_value": round(mv, 2),
        "starting_capital": round(start, 2),
        "total_pnl": round(equity - start, 2),
        "total_return_pct": round((equity / start - 1) * 100, 2),
        "today_pnl": round(equity - day_start, 2),
        "today_pnl_pct": round((equity / day_start - 1) * 100, 2) if day_start else 0.0,
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "open_positions": int(open_count),
        "closed_trades": len(pnls),
        "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else (gross_profit and 99.0 or 0.0),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "best_trade": round(max(pnls), 2) if pnls else 0.0,
        "worst_trade": round(min(pnls), 2) if pnls else 0.0,
        "max_drawdown_pct": round(max_dd, 2),
        "best_day": round(max(day_changes), 2) if day_changes else 0.0,
        "worst_day": round(min(day_changes), 2) if day_changes else 0.0,
        "green_days": sum(1 for x in day_changes if x > 0),
        "red_days": sum(1 for x in day_changes if x < 0),
        "sessions": len(day_rows),
        "day_date": acc["day_date"],
        "day_start_equity": round(day_start, 2),
        "demo": bool(acc["demo"]),
        "mode": "paper",
    }
