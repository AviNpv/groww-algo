"""Paper broker: market fills with slippage + Indian intraday charges.

No real order is placed. The broker only writes to the local SQLite ledger.
The same interface is what a live adapter would implement later.
"""
from __future__ import annotations

import math

from engine import ledger
from engine.settings import cfg, param


def _slippage() -> float:
    return float(cfg("costs", "slippage_pct", default=0.05)) / 100.0


def charges(side: str, qty: int, price: float,
            params: dict | None = None) -> float:
    """Groww intraday equity charges for one executed order.

    Brokerage is min(0.03% of turnover, the per-order cap), which is how Groww
    bills intraday equity — a flat ₹20 would make small ₹4k orders uneconomic
    and would badly distort both the paper ledger and the backtest.
    """
    turnover = max(float(price), 0.0) * max(int(qty), 0)
    if turnover <= 0:
        return 0.0
    pct = float(param(params, "costs", "brokerage_pct", 0.03)) / 100.0
    cap = float(param(params, "costs", "brokerage_per_order", 20.0))
    brokerage = min(turnover * pct, cap) if pct > 0 else cap

    exchange = turnover * float(param(params, "costs", "exchange_txn_pct", 0.00297)) / 100.0
    sebi = turnover * float(param(params, "costs", "sebi_pct", 0.0001)) / 100.0
    gst = (brokerage + exchange + sebi) * float(param(params, "costs", "gst_pct", 18.0)) / 100.0
    total = brokerage + exchange + sebi + gst

    if str(side).upper() == "SELL":
        total += turnover * float(param(params, "costs", "stt_sell_pct", 0.025)) / 100.0
    else:
        total += turnover * float(param(params, "costs", "stamp_buy_pct", 0.003)) / 100.0
    return round(total, 2)


def buy_charges(qty: int, price: float, params: dict | None = None) -> float:
    return charges("BUY", qty, price, params)


def sell_charges(qty: int, price: float, params: dict | None = None) -> float:
    return charges("SELL", qty, price, params)


def leverage(params: dict | None = None) -> float:
    """Groww-style intraday leverage multiplier (default 5x)."""
    value = float(param(params, "account", "intraday_leverage", 5))
    return value if value >= 1.0 else 1.0


def size_position(price: float, stop: float, equity: float, cash: float,
                  params: dict | None = None) -> int:
    """Risk-based sizing, capped by leveraged exposure and available margin.

    Leverage multiplies buying power and the exposure cap, not the risk budget:
    V4's risk_per_trade still decides the stop-loss loss. Cash is blocked as
    margin (order value / leverage) rather than the full notional.
    """
    risk_amount = float(param(params, "account", "risk_per_trade", 100))
    per_share = max(price - stop, 0.01)
    qty_risk = math.floor(risk_amount / per_share)

    mult = leverage(params)
    max_pct = float(param(params, "account", "max_position_pct", 45)) / 100.0
    qty_exposure = math.floor(equity * max_pct * mult / price)
    buying_power = max(cash - buy_charges(1, price, params), 0.0) * mult
    qty_cash = math.floor(buying_power / price)

    qty = max(0, min(qty_risk, qty_exposure, qty_cash))
    return int(qty)


def buy(symbol: str, price: float, qty: int, stop: float, target: float,
        score: float, reason: str) -> dict:
    fill = round(price * (1 + _slippage()), 2)
    charges = buy_charges(qty, fill)
    mult = leverage()
    notional = fill * qty
    margin = notional / mult
    cash_needed = margin + charges
    acc = ledger.account()
    cash = float(acc.get("cash", 0))
    if cash_needed > cash + 1e-6:
        return {"ok": False,
                "error": f"insufficient paper margin: need {cash_needed:.2f}, have {cash:.2f}"}

    trade_id = ledger.open_trade(symbol, qty, fill, stop, target, score, reason,
                                 charges=charges, mode="paper",
                                 leverage=mult, margin_blocked=margin)
    ledger.set_cash(cash - cash_needed)
    return {
        "ok": True, "trade_id": trade_id, "symbol": symbol, "side": "BUY",
        "qty": qty, "price": fill, "value": round(notional, 2),
        "leverage": mult, "margin": round(margin, 2),
        "charges": round(charges, 2), "stop": stop, "target": target,
    }


def sell(trade_id: int, price: float, reason: str) -> dict:
    positions = {p["trade_id"]: p for p in ledger.positions()}
    pos = positions.get(int(trade_id))
    if pos is None:
        return {"ok": False, "error": "position not found"}
    qty = int(pos["qty"])
    fill = round(price * (1 - _slippage()), 2)
    charges = sell_charges(qty, fill)
    result = ledger.close_trade(trade_id, fill, charges, reason)
    if result is None:
        return {"ok": False, "error": "position not found"}
    ledger.adjust_cash(-charges)
    return {
        "ok": True, "trade_id": int(trade_id), "symbol": pos["symbol"], "side": "SELL",
        "qty": qty, "price": fill, "charges": round(charges, 2),
        "leverage": pos.get("leverage", 1), "margin": pos.get("margin_blocked", 0),
        "pnl": result["pnl"], "reason": reason,
    }


def mark(price_map: dict[str, float]) -> None:
    """Update last price on every open position."""
    for pos in ledger.positions():
        price = price_map.get(pos["symbol"]) or price_map.get(pos["symbol"] + ".NS")
        if price:
            ledger.update_position(pos["trade_id"], last=float(price),
                                   highest=max(float(price), float(pos.get("highest_price") or 0)))
