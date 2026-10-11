"""0x Swap API v2 venue for Arc (chain 5042; Arc is listed in 0x's supported chains for Swap and Gasless).

Flow (AllowanceHolder, the endpoint 0x recommends):
  quote()          GET /swap/allowance-holder/price   indicative price, route sources, buy/sell tax, balance/allowance issues
  build_swap_tx()  GET /swap/allowance-holder/quote   firm quote with calldata for THIS wallet, re-checked before use

SAFETY (same posture as the KyberSwap venue):
  * Quoting is read-only and always available. EXECUTION stays off until you put the AllowanceHolder address you verified on-chain
    for Arc into ``ARC_RUNNER_ZEROEX_ALLOWANCE_HOLDER`` (0x publishes the address per chain; Arc is not on the cheat-sheet list yet,
    so it is never hard-coded here) and into the wallet policy's allowed routers.
  * The transaction target AND the allowance spender returned by the API must both be in that allowlist.
  * The venue only READS the USDC allowance; it never sends approvals (never approve the Settler contract).
  * 0x reports buy/sell taxes of the token: they are surfaced to the policy (SELL_TAX limit) and to the AI.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from app.chains.arc.network import USDC_ERC20_ADDRESS
from app.chains.base import FillDetails, SimulationResult, TxReceipt
from app.core.errors import DataUnavailable, IntegrationNotVerified
from app.core.types import Side
from app.domain.trade import Quote, TradeRequest, UnsignedTransaction
from app.venues.base import Venue
from app.venues.fills import parse_transfer_fill

ARC_CHAIN_ID = 5042


class ZeroExVenue(Venue):
    name = "zerox"
    label = "0x Swap API"

    def __init__(self, rpc, wallet_address: str, api_key: str, *, base_url: str = "https://api.0x.org", chain_id: int = ARC_CHAIN_ID,
                 allowance_holder_allowlist: set[str] | None = None, timeout_s: float = 8.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        if not api_key:
            raise ValueError("0x needs an API key (https://dashboard.0x.org)")
        self.rpc, self.wallet_address, self.chain_id = rpc, wallet_address, int(chain_id)
        self.base = base_url.rstrip("/")
        self._allow = {a.lower() for a in (allowance_holder_allowlist or set()) if a}
        self.http = httpx.AsyncClient(timeout=timeout_s, transport=transport,
                                      headers={"0x-api-key": api_key.strip(), "0x-version": "v2", "accept": "application/json"})
        self._dec: dict[str, int] = {}

    def router_allowlist(self) -> set[str]:
        return set(self._allow)

    def executable(self) -> tuple[bool, str]:
        if not self._allow:
            return False, "quote-only: set ARC_RUNNER_ZEROEX_ALLOWANCE_HOLDER to the AllowanceHolder address you verified on-chain for Arc"
        return True, ""

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _decimals(self, token: str) -> int:
        t = token.lower()
        if t not in self._dec:
            self._dec[t] = int(await self.rpc.call("eth_call", [{"to": t, "data": "0x313ce567"}, "latest"]), 16)
        return self._dec[t]

    async def _get(self, path: str, params: dict) -> dict:
        try:
            r = await self.http.get(f"{self.base}{path}", params=params)
        except httpx.HTTPError as exc:
            raise DataUnavailable(f"0x network error: {type(exc).__name__}") from exc
        if r.status_code == 429:
            raise DataUnavailable("0x rate limited (HTTP 429)")
        if r.status_code in (401, 403):
            raise DataUnavailable(f"0x rejected the API key (HTTP {r.status_code})")
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 400:
            msg = (body.get("message") if isinstance(body, dict) else None) or r.text[:160]
            name = body.get("name") if isinstance(body, dict) else None
            raise DataUnavailable(f"0x {path} failed (HTTP {r.status_code}{', ' + str(name) if name else ''}): {msg}")
        if not isinstance(body, dict):
            raise DataUnavailable(f"0x {path} returned an unexpected body")
        if body.get("liquidityAvailable") is False:
            raise DataUnavailable("0x reports no liquidity for this pair")
        return body

    def _params(self, request: TradeRequest, sell: str, buy: str, amount_raw: int) -> dict:
        p = {"chainId": str(self.chain_id), "sellToken": sell, "buyToken": buy, "sellAmount": str(amount_raw), "taker": self.wallet_address}
        p["slippageBps"] = str(max(1, int(round(request.max_slippage_pct * 100))))
        return p

    async def _legs(self, request: TradeRequest) -> tuple[str, str, int, int]:
        token, usdc = request.token_address.lower(), USDC_ERC20_ADDRESS.lower()
        tdec = await self._decimals(token)
        if request.side == Side.BUY:
            raw = int((Decimal(str(request.amount_usdc or 0)) * Decimal(10 ** 6)).to_integral_value())
            return usdc, token, raw, tdec
        raw = int((Decimal(str(request.quantity or 0)) * (Decimal(10) ** tdec)).to_integral_value())
        return token, usdc, raw, tdec

    # ------------------------------------------------------------------ quote
    async def quote(self, request: TradeRequest) -> Quote:
        if request.chain.lower() != "arc":
            raise IntegrationNotVerified("0x venue", "only Arc is enabled")
        sell, buy, raw, tdec = await self._legs(request)
        if raw <= 0:
            raise DataUnavailable("0x quote needs a positive amount")
        body = await self._get("/swap/allowance-holder/price", self._params(request, sell, buy, raw))
        return self._to_quote(body, request, tdec)

    def _to_quote(self, body: dict, request: TradeRequest, tdec: int) -> Quote:
        try:
            out_raw = int(str(body.get("buyAmount")))
        except (TypeError, ValueError) as exc:
            raise DataUnavailable("0x quote had no buyAmount") from exc
        expected = out_raw / 10 ** (tdec if request.side == Side.BUY else 6)
        if expected <= 0:
            raise DataUnavailable("0x returned a zero output")
        if request.side == Side.BUY:
            amount_in, price = float(request.amount_usdc or 0), float(request.amount_usdc or 0) / expected
        else:
            amount_in, price = float(request.quantity or 0), expected / float(request.quantity or 1)
        ref = request.reference_price
        # 0x v2 does not report price impact. Not guessing zero: the gap to the reference price (fees and tax included) stands in,
        # and with no reference at all the impact is 100% so the quote fails closed.
        if ref and ref > 0:
            impact = max(0.0, (price / ref - 1.0) * 100.0) if request.side == Side.BUY else max(0.0, (ref / price - 1.0) * 100.0)
        else:
            impact = 100.0
        meta = body.get("tokenMetadata") or {}
        tok = meta.get("buyToken" if request.side == Side.BUY else "sellToken") or {}
        buy_tax, sell_tax = _bps(tok.get("buyTaxBps")), _bps(tok.get("sellTaxBps"))
        fills = (body.get("route") or {}).get("fills") or []
        agg: dict[str, int] = {}
        for f in fills:
            if isinstance(f, dict):
                agg[str(f.get("source") or "?")] = agg.get(str(f.get("source") or "?"), 0) + int(f.get("proportionBps") or 0)
        route = "|".join(f"{s}:{bps / 100:g}%" for s, bps in sorted(agg.items(), key=lambda kv: -kv[1]))[:140] or None
        fees = body.get("fees") or {}
        zfee = (fees.get("zeroExFee") or {}) if isinstance(fees, dict) else {}
        issues = body.get("issues") or {}
        now = datetime.now(timezone.utc)
        return Quote(side=request.side, token_address=request.token_address.lower(), amount_in=amount_in, expected_out=expected, price=price,
                     price_impact_pct=impact, fee_usdc=0.0, router_address=str((body.get("transaction") or {}).get("to") or "").lower(),
                     venue=self.name, fee_pct=None,
                     diagnostics={"route": route, "routing": "0x-swap-v2", "impact_unit": "percent",
                                  "buy_tax_pct": buy_tax, "sell_tax_pct": sell_tax,
                                  "zero_ex_fee_amount": zfee.get("amount"), "issues": {k: bool(v) for k, v in issues.items() if k in ("allowance", "balance")},
                                  "network_fee": body.get("totalNetworkFee"), "min_out": body.get("minBuyAmount"), "zid": body.get("zid")},
                     quoted_at=now, expires_at=now + timedelta(seconds=20), source="0x-swap-v2")

    # ------------------------------------------------------------------ build / simulate / fill
    async def build_swap_tx(self, quote: Quote, request: TradeRequest, deadline: datetime) -> UnsignedTransaction:
        ok, why = self.executable()
        if not ok:
            raise IntegrationNotVerified("0x execution", why)
        sell, buy, raw, tdec = await self._legs(request)
        body = await self._get("/swap/allowance-holder/quote", self._params(request, sell, buy, raw))
        fresh = self._to_quote(body, request, tdec)
        if fresh.expected_out < quote.expected_out * (1 - request.max_slippage_pct / 100):
            raise DataUnavailable(f"0x firm quote moved: {fresh.expected_out:.8g} < accepted {quote.expected_out:.8g} beyond slippage")
        tx = body.get("transaction") or {}
        to, data = str(tx.get("to") or "").lower(), str(tx.get("data") or "")
        if to not in self._allow:
            raise IntegrationNotVerified("0x AllowanceHolder", f"transaction target {to or 'none'} is not in the verified allowlist")
        if not data.startswith("0x") or len(data) < 10:
            raise DataUnavailable("0x quote returned empty calldata")
        try:
            value = int(str(tx.get("value") or "0"), 0)
        except ValueError:
            value = 0
        if value != 0:
            raise IntegrationNotVerified("0x value", "quote asked for native value on a USDC/ERC-20 swap")
        issues = body.get("issues") or {}
        if issues.get("balance"):
            raise IntegrationNotVerified("0x balance", "wallet balance is below the sell amount according to 0x")
        spender = str(((issues.get("allowance") or {}).get("spender")) or body.get("allowanceTarget") or "").lower()
        if spender and spender not in self._allow:
            raise IntegrationNotVerified("0x allowance target", f"spender {spender} is not in the verified allowlist")
        if request.side == Side.BUY and spender:     # plain ERC-20 allowance of USDC to AllowanceHolder must already exist
            enc = "0xdd62ed3e" + self.wallet_address.lower().removeprefix("0x").rjust(64, "0") + spender.removeprefix("0x").rjust(64, "0")
            allowance = int(await self.rpc.call("eth_call", [{"to": USDC_ERC20_ADDRESS, "data": enc}, "latest"]), 16)
            if allowance < raw:
                raise IntegrationNotVerified("0x USDC allowance",
                                             f"wallet has approved only {allowance / 1e6:.4f} USDC to AllowanceHolder; approve it once, then retry")
        return UnsignedTransaction(chain="arc", chain_id=self.chain_id, to=to, data=data, value=0, token_address=request.token_address,
                                   side=request.side, amount_usdc=request.amount_usdc or 0.0,
                                   min_out=quote.expected_out * (1 - request.max_slippage_pct / 100), slippage_pct=request.max_slippage_pct,
                                   deadline=deadline, function_signature=None, params=[],
                                   idempotency_key=request.idempotency_key)

    async def simulate(self, tx: UnsignedTransaction) -> SimulationResult:
        try:
            await self.rpc.call("eth_call", [{"from": self.wallet_address, "to": tx.to, "data": tx.data, "value": hex(tx.value)}, "latest"])
            return SimulationResult(ok=True, detail="Arc RPC eth_call succeeded")
        except Exception as exc:  # noqa: BLE001
            return SimulationResult(ok=False, detail=str(exc)[:500])

    async def parse_fill(self, receipt: TxReceipt, quote: Quote) -> FillDetails:
        return parse_transfer_fill(self.wallet_address, receipt, quote, token_decimals=await self._decimals(quote.token_address))


def _bps(v) -> float | None:
    try:
        return round(int(str(v)) / 100.0, 4)
    except (TypeError, ValueError):
        return None
