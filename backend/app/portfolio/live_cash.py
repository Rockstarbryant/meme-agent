"""LIVE spendable cash.

In LIVE mode the real wallet is the source of truth for cash. The agent may spend at most
``min(wallet USDC balance, allocated capital - capital already in open positions)``.
Pure function, shared by the runner (sizing / risk engine) and the API (dashboard / wallet page) so the
number the user sees is exactly the number the risk engine uses.
"""
from __future__ import annotations


def spendable_usdc(wallet_balance: float, allocated_capital: float | None, open_exposure: float = 0.0) -> float:
    """Cash the agent can still deploy. Never negative; ``allocated_capital`` of None/0 means "no cap"."""
    cap = max(0.0, float(wallet_balance or 0.0))
    if allocated_capital is not None and float(allocated_capital) > 0:
        cap = min(cap, max(0.0, float(allocated_capital) - max(0.0, float(open_exposure or 0.0))))
    return round(cap, 6)
