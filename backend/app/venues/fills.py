"""Fill reconciliation from ERC-20 Transfer logs (wallet-centric), shared by aggregator venues."""
from __future__ import annotations

from app.chains.arc.network import USDC_ERC20_ADDRESS
from app.chains.base import FillDetails, TxReceipt
from app.core.errors import IntegrationNotVerified
from app.core.types import Side
from app.domain.trade import Quote

_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def parse_transfer_fill(wallet_address: str, receipt: TxReceipt, quote: Quote, *, token_decimals: int = 18) -> FillDetails:
    wallet = wallet_address.lower().removeprefix("0x")
    usdc, token = USDC_ERC20_ADDRESS.lower(), str(quote.token_address).lower()
    usdc_in = usdc_out = tok_in = tok_out = 0.0
    for log in receipt.logs or []:
        topics = log.get("topics") or []
        if len(topics) < 3 or str(topics[0]).lower() != _TRANSFER:
            continue
        try:
            frm, to = str(topics[1])[-40:].lower(), str(topics[2])[-40:].lower()
            raw, addr = int(log.get("data") or "0x0", 16), str(log.get("address") or "").lower()
        except (TypeError, ValueError):
            continue
        if addr == usdc:
            amt = raw / 10 ** 6
            usdc_in += amt if to == wallet else 0.0
            usdc_out += amt if frm == wallet else 0.0
        elif addr == token:
            amt = raw / 10 ** token_decimals
            tok_in += amt if to == wallet else 0.0
            tok_out += amt if frm == wallet else 0.0
    if quote.side == Side.BUY:
        qty, usdc_amt = tok_in, usdc_out
        if qty <= 0 or usdc_amt <= 0:
            qty, usdc_amt = float(quote.expected_out or 0), float(quote.amount_in or 0)
    else:
        qty, usdc_amt = tok_out, usdc_in
        if qty <= 0 or usdc_amt <= 0:
            qty, usdc_amt = float(quote.amount_in or 0), float(quote.expected_out or 0)
    if qty <= 0 or usdc_amt <= 0:
        raise IntegrationNotVerified("fill parsing", "could not derive the fill from Transfer logs or the quote")
    fee = float(quote.fee_usdc or 0.0)
    return FillDetails(filled_quantity=qty, avg_price=usdc_amt / qty, fee_usdc=fee if fee > 0 else usdc_amt * 0.003)
