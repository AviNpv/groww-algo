"""The automation loop: scan → size → paper-trade → manage exits → square off."""
from __future__ import annotations

import datetime as dt
import threading
import traceback

from engine import broker, ledger, marketdata, scanner, strategy
from engine.settings import cfg


class AlgoEngine:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._scan_lock = threading.Lock()
        self.state = "stopped"
        self.last_scan_at: dt.datetime | None = None
        self.last_scan: dict | None = None
        self.last_error: str | None = None
        self.next_scan_at: dt.datetime | None = None
        self.last_cycle_at: dt.datetime | None = None
        self.cycles = 0

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> dict:
        from engine.settings import now_ist

        with self._lock:
            if self.running:
                return self.status()
            self._stop.clear()
            self.state = "running"
            self._thread = threading.Thread(target=self._loop, name="algo-engine", daemon=True)
            self._thread.start()
        return self.status()

    def stop(self) -> dict:
        with self._lock:
            self._stop.set()
            self.state = "stopped"
        if self._thread:
            self._thread.join(timeout=5)
        return self.status()

    # ------------------------------------------------------------------
    # loop
    # ------------------------------------------------------------------
    def _loop(self) -> None:
        from engine.settings import in_entry_window, is_market_open, now_ist

        try:
            ledger.ensure_day()
            self._bootstrap_scan()
        except Exception as exc:  # pragma: no cover - startup guard
            self.last_error = f"startup: {exc}"

        while not self._stop.is_set():
            try:
                self.cycle()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            self._stop.wait(20)

    def _bootstrap_scan(self) -> None:
        """Populate the Scanner tab even before/after market hours (no trades)."""
        from engine.settings import now_ist

        try:
            self.run_scan(autotrade=False)
            self.last_scan_at = now_ist()
        except Exception as exc:
            self.last_error = f"bootstrap scan: {exc}"

    def cycle(self) -> dict:
        from engine.settings import in_entry_window, is_market_open, now_ist

        now = now_ist()
        self.last_cycle_at = now
        self.cycles += 1
        action = {"ts": now.isoformat(timespec="seconds"), "trades": [], "exits": []}

        ledger.ensure_day()

        if is_market_open(now):
            action["exits"] = self.manage_positions(now)

            due = self.next_scan_at is None or now >= self.next_scan_at
            in_window = in_entry_window(now)
            auto = bool(cfg("engine", "autotrade", default=True))
            if not bool(cfg("engine", "allow_new_entries", default=True)):
                auto = False
            if not bool(cfg("engine", "allow_long", default=True)):
                auto = False
            if in_window and due:
                result = self.run_scan(autotrade=auto)
                action["scan"] = {
                    "qualified": result.get("qualified"),
                    "scanned": result.get("scanned"),
                    "signals": result.get("signals", []),
                }
                interval = int(cfg("engine", "scan_interval_sec", default=300))
                self.next_scan_at = now + dt.timedelta(seconds=interval)

            self._maybe_square_off(now, action)
            ledger.snapshot_equity(now.date().isoformat())
        else:
            # Safety net: a position must never survive past the square-off time.
            # This covers a stopped API, a crash, or a weekend restart.
            self._catchup_square_off(now, action)

        return action

    # ------------------------------------------------------------------
    # scanning + entries
    # ------------------------------------------------------------------
    def run_scan(self, autotrade: bool = True) -> dict:
        from engine.settings import now_ist

        with self._scan_lock:
            result = scanner.scan()
            result["signals"] = []
            result["trade_errors"] = []

            held = {p["symbol"] for p in ledger.positions()}
            equity = ledger.current_equity()
            cash = float(ledger.account().get("cash", 0))

            # persist every qualifying/watchlist observation
            observations = []
            for c in result["results"][:40]:
                if c["status"] == "REJECT":
                    continue
                signal = strategy.build_entry(c, equity, cash, held)
                observations.append({**signal, "buckets": c.get("buckets", {}),
                                     "patterns": c.get("patterns", []),
                                     "status": c.get("status")})
            ledger.record_signals(observations)
            result["signals"] = observations

            if autotrade:
                allowed, reason = strategy.can_open_new(equity)
                if not allowed:
                    result["trade_errors"].append(reason)
                else:
                    for signal in observations:
                        if signal.get("action") != "BUY":
                            continue
                        if signal["symbol"] in held:
                            continue
                        allowed, reason = strategy.can_open_new(ledger.current_equity())
                        if not allowed:
                            result["trade_errors"].append(reason)
                            break
                        order = broker.buy(
                            signal["symbol"], signal["price"], signal["qty"],
                            signal["stop"], signal["target"], signal.get("score", 0),
                            signal.get("reason", ""),
                        )
                        if order.get("ok"):
                            held.add(signal["symbol"])
                            signal["executed"] = order
                            result["trade_errors"].append(
                                f"BUY {order['symbol']} {order['qty']} @ {order['price']}"
                            )
                        else:
                            result["trade_errors"].append(f"{signal['symbol']}: {order.get('error')}")

            result["equity"] = ledger.current_equity()
            ledger.record_scan(result["duration_ms"], result["scanned"],
                               result["qualified"], result["watchlist"],
                               result["source"], None)
            ledger.snapshot_equity(now_ist().date().isoformat())
            self.last_scan = result
            self.last_scan_at = now_ist()
            self.last_error = None
            return result

    def scan_now(self, autotrade: bool = False) -> dict:
        return self.run_scan(autotrade=autotrade)

    # ------------------------------------------------------------------
    # exits
    # ------------------------------------------------------------------
    def manage_positions(self, now: dt.datetime | None = None) -> list[dict]:
        from engine.settings import now_ist

        now = now or now_ist()
        open_positions = ledger.positions()
        if not open_positions:
            return []

        price_map = marketdata.prices([p["symbol"] for p in open_positions])
        broker.mark(price_map)
        exits: list[dict] = []

        for pos in ledger.positions():
            price = price_map.get(pos["symbol"])
            if not price:
                price = float(pos.get("last_price") or pos["avg_price"])
            decision = strategy.check_exit(pos, float(price), now)
            if decision["new_stop"] and decision["new_stop"] != pos.get("stop"):
                ledger.update_position(pos["trade_id"], stop=decision["new_stop"])
            if decision["exit"]:
                order = broker.sell(pos["trade_id"], float(price), decision["reason"])
                if order.get("ok"):
                    exits.append(order)
        return exits

    def _maybe_square_off(self, now: dt.datetime, action: dict) -> None:
        square = cfg("exit", "square_off", default="15:20")
        hh, mm = int(square[:2]), int(square[3:5])
        if now.hour > hh or (now.hour == hh and now.minute >= mm):
            for pos in ledger.positions():
                price = float(pos.get("last_price") or pos["avg_price"])
                order = broker.sell(pos["trade_id"], price, "SQUARE_OFF")
                if order.get("ok"):
                    action["exits"].append(order)

    def _catchup_square_off(self, now: dt.datetime, action: dict) -> None:
        """Close anything still open once the session is over.

        Only runs after the square-off time on a trading day, or on any day the
        market is shut (e.g. a position left open when the API was killed).
        """
        from engine.settings import is_trading_day

        positions = ledger.positions()
        if not positions:
            return
        square = cfg("exit", "square_off", default="15:20")
        hh, mm = int(square[:2]), int(square[3:5])
        past_square_off = now.hour > hh or (now.hour == hh and now.minute >= mm)
        if is_trading_day(now.date()) and not past_square_off:
            return  # still inside the session; normal exit logic owns these

        price_map: dict[str, float] = {}
        try:
            price_map = marketdata.prices([p["symbol"] for p in positions])
        except Exception:
            price_map = {}
        for pos in positions:
            price = price_map.get(pos["symbol"]) or float(pos.get("last_price") or pos["avg_price"])
            order = broker.sell(pos["trade_id"], float(price), "SQUARE_OFF")
            if order.get("ok"):
                action["exits"].append(order)
        if action["exits"]:
            ledger.snapshot_equity()

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def status(self) -> dict:
        from engine.settings import in_entry_window, is_market_open, now_ist

        health = marketdata.health()
        now = now_ist()
        return {
            "state": "running" if self.running else "stopped",
            "mode": "paper",
            "demo": ledger.is_demo(),
            "market_open": is_market_open(now),
            "entry_window": in_entry_window(now),
            "now_ist": now.isoformat(timespec="seconds"),
            "last_scan_at": self.last_scan_at.isoformat(timespec="seconds") if self.last_scan_at else None,
            "next_scan_at": self.next_scan_at.isoformat(timespec="seconds") if self.next_scan_at else None,
            "last_cycle_at": self.last_cycle_at.isoformat(timespec="seconds") if self.last_cycle_at else None,
            "position_count": len(ledger.positions()),
            "equity": ledger.current_equity(),
            "data_source": {
                "active": health.get("active"),
                "groww_ok": bool(health.get("ok")),
                "state": health.get("state"),
                "message": health.get("message"),
                "fallback_reason": health.get("fallback_reason"),
            },
            "last_error": self.last_error,
        }


ENGINE = AlgoEngine()
