"""KyberSwap Aggregator venue for Arc (slug ``arc``, chain 5042).

KyberSwap's aggregator lists Arc (``GET /arc/api/v1/routes`` answers ``code: 0``), so it is a second source of routing and
prices next to the Uniswap Trading API. Two API calls: ``/routes`` (quote) then ``/route/build`` (calldata).

SAFETY: quoting is read-only and always available. EXECUTION is off until you put the router address that you verified on-chain
into ``ARC_RUNNER_KYBERSWAP_ROUTER_ALLOWLIST`` (and in the wallet policy's allowed routers). The router address the API returns
is never trusted on its own. KyberSwap pulls USDC with a plain ERC-20 allowance, so the wallet must already have approved the
router: this venue only READS the allowance and refuses (with a clear message) when it is too low; it never sends approvals.
"""
from __future__ import annotations

import time
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


class KyberSwapVenue(Venue):
    name = "kyberswap"
    label = "KyberSwap Aggregator"

    def __init__(self, rpc, wallet_address: str, *, base_url: str = "https://aggregator-api.kyberswap.com", chain_slug: str = "arc",
                 client_id: str = "arc-autonomous-trading-agent", router_allowlist: set[str] | None = None, timeout_s: float = 8.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.rpc, self.wallet_address = rpc, wallet_address
        self.base = f"{base_url.rstrip('/')}/{chain_slug}/api/v1"
        self._allow = {a.lower() for a in (router_allowlist or set()) if a}
        self.http = httpx.AsyncClient(timeout=timeout_s, transport=transport,
                                      headers={"accept": "application/json", "x-client-id": client_id})
        self._last: dict[str, dict] = {}     # token -> last routeSummary (for /route/build)
        self._dec: dict[str, int] = {}

    # ------------------------------------------------------------------ policy
    def router_allowlist(self) -> set[str]:
        return set(self._allow)

    def executable(self) -> tuple[bool, str]:
        if not self._allow:
            return False, "quote-only: set ARC_RUNNER_KYBERSWAP_ROUTER_ALLOWLIST to the router address you verified on-chain"
        return True, ""

    async def aclose(self) -> None:
        await self.http.aclose()

    # ------------------------------------------------------------------ helpers
    async def _decimals(self, token: str) -> int:
        t = token.lower()
        if t not in self._dec:
            self._dec[t] = int(await self.rpc.call("eth_call", [{"to": t, "data": "0x313ce567"}, "latest"]), 16)
        return self._dec[t]

    async def _get(self, path: str, params: dict) -> dict:
        try:
            r = await self.http.get(f"{self.base}{path}", params=params)
        except httpx.HTTPError as exc:
            raise DataUnavailable(f"KyberSwap network error: {type(exc).__name__}") from exc
        return self._unwrap(r, path)

    @staticmethod
    def _unwrap(r: httpx.Response, path: str) -> dict:
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code == 429:
            raise DataUnavailable("KyberSwap rate limited (HTTP 429)")
        if r.status_code >= 400 or (isinstance(body, dict) and body.get("code") not in (0, None)):
            msg = (body.get("message") if isinstance(body, dict) else None) or r.text[:160]
            raise DataUnavailable(f"KyberSwap {path} failed (HTTP {r.status_code}, code {body.get('code') if isinstance(body, dict) else '?'}): {msg}")
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise DataUnavailable(f"KyberSwap {path} returned no data")
        return data

    # ------------------------------------------------------------------ quote
    async def quote(self, request: TradeRequest) -> Quote:
        if request.chain.lower() != "arc":
            raise IntegrationNotVerified("KyberSwap venue", "only Arc is enabled")
        token = request.token_address.lower()
        usdc = USDC_ERC20_ADDRESS.lower()
        tdec = await self._decimals(token)
        if request.side == Side.BUY:
            token_in, token_out = usdc, token
            amount_raw = int((Decimal(str(request.amount_usdc or 0)) * Decimal(10 ** 6)).to_integral_value())
        else:
            token_in, token_out = token, usdc
            amount_raw = int((Decimal(str(request.quantity or 0)) * (Decimal(10) ** tdec)).to_integral_value())
        if amount_raw <= 0:
            raise DataUnavailable("KyberSwap quote needs a positive amount")
        data = await self._get("/routes", {"tokenIn": token_in, "tokenOut": token_out, "amountIn": str(amount_raw), "gasInclude": "true"})
        rs = data.get("routeSummary") or {}
        try:
            out_raw = int(str(rs.get("amountOut")))
        except (TypeError, ValueError) as exc:
            raise DataUnavailable("KyberSwap quote had no amountOut") from exc
        router = str(data.get("routerAddress") or "").lower()
        expected = out_raw / 10 ** (tdec if request.side == Side.BUY else 6)
        if expected <= 0:
            raise DataUnavailable("KyberSwap returned a zero output")
        if request.side == Side.BUY:
            amount_in, price = float(request.amount_usdc or 0), float(request.amount_usdc or 0) / expected
        else:
            amount_in, price = float(request.quantity or 0), expected / float(request.quantity or 1)
        # Price impact: Kyber reports USD amounts only when it can price the token. When it cannot we do NOT guess zero: the
        # impact is taken as the full gap to the reference price (fees included), which fails closed.
        in_usd, out_usd = _f(rs.get("amountInUsd")), _f(rs.get("amountOutUsd"))
        ref = request.reference_price
        if in_usd and out_usd and in_usd > 0 and out_usd > 0:
            impact = max(0.0, (1.0 - out_usd / in_usd) * 100.0)
        elif ref and ref > 0:
            impact = max(0.0, (price / ref - 1.0) * 100.0) if request.side == Side.BUY else max(0.0, (ref / price - 1.0) * 100.0)
        else:
            impact = 100.0
        legs: list[str] = []
        for hop in rs.get("route") or []:
            for leg in (hop if isinstance(hop, list) else [hop]):
                if isinstance(leg, dict):
                    legs.append(f"{leg.get('exchange') or leg.get('poolType') or '?'}:?")
        self._last[token] = {"routeSummary": rs, "router": router, "side": request.side.value, "amount_raw": amount_raw}
        return Quote(side=request.side, token_address=token, amount_in=amount_in, expected_out=expected, price=price,
                     price_impact_pct=impact, fee_usdc=0.0, router_address=router, venue=self.name, fee_pct=None,
                     diagnostics={"route": "|".join(legs)[:120] or None, "routing": "kyberswap-aggregator",
                                  "price_impact_raw": {"in_usd": in_usd, "out_usd": out_usd}, "impact_unit": "percent",
                                  "gas_usd": _f(rs.get("gasUsd"))},
                     quoted_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc) + timedelta(seconds=20),
                     source="kyberswap-aggregator")

    # ------------------------------------------------------------------ build / simulate / fill
    async def build_swap_tx(self, quote: Quote, request: TradeRequest, deadline: datetime) -> UnsignedTransaction:
        ok, why = self.executable()
        if not ok:
            raise IntegrationNotVerified("KyberSwap execution", why)
        cached = self._last.get(request.token_address.lower())
        if not cached or cached["side"] != request.side.value:
            raise DataUnavailable("no matching KyberSwap quote is cached")
        if cached["router"] not in self._allow:
            raise IntegrationNotVerified("KyberSwap router", f"router {cached['router']} is not in the verified allowlist")
        if request.side == Side.BUY:      # plain ERC-20 allowance of USDC to the router must already exist
            data = "0xdd62ed3e" + self.wallet_address.lower().removeprefix("0x").rjust(64, "0") + cached["router"].removeprefix("0x").rjust(64, "0")
            allowance = int(await self.rpc.call("eth_call", [{"to": USDC_ERC20_ADDRESS, "data": data}, "latest"]), 16)
            if allowance < cached["amount_raw"]:
                raise IntegrationNotVerified("KyberSwap USDC allowance",
                                             f"wallet has approved only {allowance / 1e6:.4f} USDC to the router; approve it once, then retry")
        slippage_bps = max(1, int(round(request.max_slippage_pct * 100)))
        built = await self._post("/route/build", {"routeSummary": cached["routeSummary"], "sender": self.wallet_address,
                                                  "recipient": self.wallet_address, "slippageTolerance": slippage_bps,
                                                  "deadline": int(deadline.timestamp()), "source": "arc-autonomous-trading-agent"})
        to, calldata = str(built.get("routerAddress") or "").lower(), str(built.get("data") or "")
        if to != cached["router"] or to not in self._allow:
            raise IntegrationNotVerified("KyberSwap router", f"build returned unexpected target {to}")
        if not calldata.startswith("0x") or len(calldata) < 10:
            raise DataUnavailable("KyberSwap build returned empty calldata")
        try:
            value = int(str(built.get("transactionValue") or "0"), 0)
        except ValueError:
            value = 0
        if value != 0:
            raise IntegrationNotVerified("KyberSwap value", "build asked for native value on a USDC-denominated swap")
        return UnsignedTransaction(chain="arc", chain_id=5042, to=to, data=calldata, value=0, token_address=request.token_address,
                                   side=request.side, amount_usdc=request.amount_usdc or 0.0,
                                   min_out=quote.expected_out * (1 - request.max_slippage_pct / 100), slippage_pct=request.max_slippage_pct,
                                   deadline=deadline, function_signature=None, params=[])

    async def _post(self, path: str, payload: dict) -> dict:
        try:
            r = await self.http.post(f"{self.base}{path}", json=payload)
        except httpx.HTTPError as exc:
            raise DataUnavailable(f"KyberSwap network error: {type(exc).__name__}") from exc
        return self._unwrap(r, path)

    async def simulate(self, tx: UnsignedTransaction) -> SimulationResult:
        try:
            await self.rpc.call("eth_call", [{"from": self.wallet_address, "to": tx.to, "data": tx.data, "value": hex(tx.value)}, "latest"])
            return SimulationResult(ok=True, detail="Arc RPC eth_call succeeded")
        except Exception as exc:  # noqa: BLE001
            return SimulationResult(ok=False, detail=str(exc)[:500])

    async def parse_fill(self, receipt: TxReceipt, quote: Quote) -> FillDetails:
        dec = await self._decimals(quote.token_address)
        return parse_transfer_fill(self.wallet_address, receipt, quote, token_decimals=dec)


def _f(v) -> float | None:
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None
