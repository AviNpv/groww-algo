"""Clear the ledger so real paper trading starts from a clean ₹10,000 base.

Run:  .venv\\Scripts\\python.exe -m scripts.reset
"""
from __future__ import annotations

from engine import ledger
from engine.settings import cfg


def main() -> None:
    capital = float(cfg("account", "starting_capital", default=10000))
    ledger.reset(starting_capital=capital, demo=False)
    print(f"Ledger reset. Cash = {capital:,.2f}, DEMO banner off.")
    print("The bot now builds its own history from the sessions it trades.")


if __name__ == "__main__":
    main()
