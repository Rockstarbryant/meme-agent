"""Read the user's real on-chain USDC balance for the dashboard / wallet page (LIVE mode)."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models as M

log = logging.getLogger(__name__)


async def live_wallet_balance(db: AsyncSession, chain, user_id: str, timeout_s: float = 5.0) -> float | None:
    """ERC-20 (6-decimal) USDC balance of the wallet the agent trades from, or None when it cannot be read.

    The per-user cloud (Privy) wallet is preferred: it is the one the worker spends from. Never raises."""
    try:
        wallets = (await db.execute(select(M.Wallet).where(M.Wallet.user_id == user_id))).scalars().all()
        real = [w for w in wallets if w.address and w.address != "PAPER" and w.address.lower().startswith("0x")]
        if not real:
            return None
        real.sort(key=lambda w: (bool(w.provider == "privy" and w.external_id), bool(w.ownership_verified), w.created_at),
                  reverse=True)
        return float(await asyncio.wait_for(chain.usdc_balance(real[0].address), timeout_s))
    except Exception as exc:  # noqa: BLE001 - display helper, must never break the page
        log.warning("live wallet balance unavailable: %s: %s", type(exc).__name__, exc)
        return None
