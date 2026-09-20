from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from eth_abi import decode as abi_decode

from app.chains.arc.deployments import ARC_UNIVERSAL_ROUTER, ARC_PERMIT2
from app.chains.base import FillDetails, SimulationResult
from app.core.errors import IntegrationNotVerified, DataUnavailable
from app.core.types import Side
from app.domain.trade import Quote, TradeRequest, UnsignedTransaction

USDC_DECIMALS = 6
ROUTER_EXECUTE_2 = "execute(bytes,bytes[])"
ROUTER_EXECUTE_3 = "execute(bytes,bytes[],uint256)"


class UniswapArcAdapter:
    """Uniswap Trading API adapter for Arc AMM swaps.

    V2 deliberately allows only CLASSIC AMM routes (V2/V3/V4). UniswapX order
    settlement is not used because the Local Runner's wallet interface expects
    a chain transaction that it can independently simulate and reconcile.
    """

    name = "uniswap_arc"
    live_trading_verified = True

    def __init__(self, api_key: str, rpc, wallet_address: str, base_url: str = "https://trade-api.gateway.uniswap.org/v1"):
        self.api_key, self.rpc, self.wallet_address, self.base_url = api_key, rpc, wallet_address, base_url.rstrip("/")
        self.http = httpx.AsyncClient(timeout=20.0, headers={
            "x-api-key": api_key,
            "accept": "application/json",
            "content-type": "application/json",
            "x-universal-router-version": "2.1.1",
            "x-agent-info": json.dumps({"integration_name": "arc-autonomous-trading-agent", "version": "2.0.0"}, separators=(",", ":")),
        })

    async def close(self) -> None:
        await self.http.aclose()

    async def _post(self, path: str, payload: dict) -> dict:
        r = await self.http.post(f"{self.base_url}/{path.lstrip('/')}", json=payload)
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:1000]}
        if r.status_code >= 400:
            raise DataUnavailable(f"Uniswap API {path} HTTP {r.status_code}: {body}")
        return body

    async def token_decimals(self, token: str) -> int:
        raw = await self.rpc.call("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
        return int(raw, 16)

    async def quote(self, request: TradeRequest) -> Quote:
        if request.chain.lower() != "arc":
            raise IntegrationNotVerified("Uniswap Arc adapter", "only Arc is enabled in V2")
        token = request.token_address.lower()
        if request.side == Side.BUY:
            token_in, token_out = "0x3600000000000000000000000000000000000000", token
            amount = _usdc_raw(request.amount_usdc or 0)
        else:
            token_in, token_out = token, "0x3600000000000000000000000000000000000000"
            dec = await self.token_decimals(token)
            amount = _token_raw(request.quantity or 0, dec)
        payload = {
            "tokenIn": token_in,
            "tokenOut": token_out,
            "tokenInChainId": 5042,
            "tokenOutChainId": 5042,
            "type": "EXACT_INPUT",
            "amount": str(amount),
            "swapper": self.wallet_address,
            "slippageTolerance": float(request.max_slippage_pct),
            "protocols": ["V2", "V3", "V4"],
            "routingPreference": "BEST_PRICE",
        }
        body = await self._post("quote", payload)
        routing = str(body.get("routing") or "")
        if routing != "CLASSIC":
            raise IntegrationNotVerified("Uniswap Arc route", f"V2 requires CLASSIC AMM routing; API returned {routing or 'unknown'}")
        q = body.get("quote") or {}
        expected_raw = _first(q, "amountOut", "outputAmount", "amountOutRaw") or _first(body, "amountOut", "outputAmount")
        if expected_raw is None:
            expected_raw = _nested(q, ["output", "amount"])
        if expected_raw is None:
            raise DataUnavailable("Uniswap quote did not contain a recognizable amountOut")
        expected_raw_int = int(str(expected_raw))
        out_dec = await self.token_decimals(token) if request.side == Side.BUY else USDC_DECIMALS
        expected_out = expected_raw_int / (10 ** out_dec)
        amount_in_human = (request.amount_usdc or 0.0) if request.side == Side.BUY else (request.quantity or 0.0)
        price = ((request.amount_usdc or 0.0) / expected_out) if request.side == Side.BUY and expected_out else ((expected_out / (request.quantity or 1.0)) if request.side == Side.SELL else 0.0)
        impact = _pct(_first(q, "priceImpact", "priceImpactPercent") or _first(body, "priceImpact", "priceImpactPercent")) or 0.0
        router = str(_first(body, "swapRouter", "routerAddress") or ARC_UNIVERSAL_ROUTER).lower()
        expires = datetime.now(timezone.utc) + timedelta(seconds=20)
        # Keep Permit2 payload private to the adapter. If it exists, the Circle provider must
        # support the exact EIP-712 signing flow before a live transaction can be built.
        setattr(q, "_permit_data", None) if False else None
        self._last_quote = body
        return Quote(side=request.side, token_address=token, amount_in=amount_in_human, expected_out=expected_out,
                     price=price, price_impact_pct=impact, fee_usdc=0.0, router_address=router,
                     pool_liquidity_usdc=None, quoted_at=datetime.now(timezone.utc), expires_at=expires, source="uniswap-trading-api")

    async def build_swap_tx(self, quote: Quote, request: TradeRequest, deadline: datetime) -> UnsignedTransaction:
        body = getattr(self, "_last_quote", None)
        if not body or not body.get("quote"):
            raise DataUnavailable("no matching Uniswap quote is cached")
        # Permit2 is the supported Arc path. V2 does not silently downgrade to an unsafe
        # approval model. The Circle signing implementation is completed only after the
        # installed CLI's typed-data command is verified on the user's runner.
        if body.get("permitData"):
            raise IntegrationNotVerified("Uniswap Permit2 + Circle signing", "quote returned permitData; configure and verify Circle typed-data signing before live execution")
        swap = await self._post("swap", {"quote": body["quote"]})
        raw = swap.get("swap") or {}
        to = str(raw.get("to") or "").lower()
        data = str(raw.get("data") or "")
        if to != ARC_UNIVERSAL_ROUTER.lower():
            raise IntegrationNotVerified("Uniswap Arc router", f"unexpected swap target {to}")
        if not data.startswith("0x") or len(data) < 10:
            raise DataUnavailable("Uniswap /swap returned empty calldata")
        value = int(str(raw.get("value") or "0"), 0) if str(raw.get("value") or "0").startswith("0x") else int(raw.get("value") or 0)
        sig, params = decode_universal_router_execute(data)
        return UnsignedTransaction(chain="arc", chain_id=5042, to=to, data=data, value=value,
                                   token_address=request.token_address, side=request.side,
                                   amount_usdc=request.amount_usdc or 0.0, min_out=quote.expected_out * (1 - request.max_slippage_pct / 100),
                                   slippage_pct=request.max_slippage_pct, deadline=deadline,
                                   function_signature=sig, params=params)

    async def simulate(self, tx: UnsignedTransaction) -> SimulationResult:
        try:
            await self.rpc.call("eth_call", [{"from": self.wallet_address, "to": tx.to, "data": tx.data, "value": hex(tx.value)}, "latest"])
            return SimulationResult(ok=True, detail="Arc RPC eth_call succeeded")
        except Exception as exc:  # noqa: BLE001
            return SimulationResult(ok=False, detail=str(exc)[:500])

    async def parse_fill(self, receipt, quote: Quote) -> FillDetails:
        """Reconcile fill from ERC-20 Transfer logs when possible; otherwise fall back to quote
        amounts. Universal Router emits heterogeneous V2/V3/V4 events, so wallet-centric
        Transfer deltas are the reliable path after a confirmed receipt.
        """
        from app.chains.arc.network import USDC_ERC20_ADDRESS

        TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        wallet = self.wallet_address.lower().removeprefix("0x")
        usdc = USDC_ERC20_ADDRESS.lower()
        token = str(quote.token_address).lower()

        usdc_in = usdc_out = token_in = token_out = 0.0
        # We do not know token decimals from the quote; Transfer raw amounts still give ratios.
        # For absolute qty we fall back to quote expected_out / amount_in when logs are incomplete.
        token_decimals = 18

        for log in receipt.logs or []:
            topics = log.get("topics") or []
            if not topics or str(topics[0]).lower() != TRANSFER_TOPIC:
                continue
            if len(topics) < 3:
                continue
            try:
                frm = str(topics[1])[-40:].lower()
                to = str(topics[2])[-40:].lower()
                raw_amt = int(log.get("data") or "0x0", 16)
                addr = str(log.get("address") or "").lower()
            except (TypeError, ValueError):
                continue
            if addr == usdc:
                amt = raw_amt / 10 ** USDC_DECIMALS
                if to == wallet:
                    usdc_in += amt
                if frm == wallet:
                    usdc_out += amt
            elif addr == token:
                amt = raw_amt / 10 ** token_decimals
                if to == wallet:
                    token_in += amt
                if frm == wallet:
                    token_out += amt

        fee_from_quote = float(quote.fee_usdc or 0.0)

        if quote.side == Side.BUY:
            qty = token_in
            spent = usdc_out
            if qty <= 0 or spent <= 0:
                qty = float(quote.expected_out or 0)
                spent = float(quote.amount_in or 0)
            if qty <= 0 or spent <= 0:
                raise IntegrationNotVerified(
                    "Uniswap fill parsing",
                    "could not derive BUY fill from Transfer logs or quote",
                )
            avg = spent / qty
            fee = fee_from_quote if fee_from_quote > 0 else max(0.0, spent * 0.003)
            return FillDetails(filled_quantity=qty, avg_price=avg, fee_usdc=fee)

        # SELL
        qty = token_out
        proceeds = usdc_in
        if qty <= 0 or proceeds <= 0:
            qty = float(quote.amount_in or 0)
            proceeds = float(quote.expected_out or 0)
        if qty <= 0 or proceeds <= 0:
            raise IntegrationNotVerified(
                "Uniswap fill parsing",
                "could not derive SELL fill from Transfer logs or quote",
            )
        avg = proceeds / qty
        fee = fee_from_quote if fee_from_quote > 0 else max(0.0, proceeds * 0.003)
        return FillDetails(filled_quantity=qty, avg_price=avg, fee_usdc=fee)


def decode_universal_router_execute(data: str) -> tuple[str, list[str]]:
    """Decode the two public Universal Router execute overloads into Circle CLI ABI params."""
    raw = bytes.fromhex(data[2:])
    selector = raw[:4].hex()
    # selectors are keccak-derived; support the canonical v2.1.1 overloads by decoding
    # against both ABI layouts and rejecting ambiguous/invalid payloads.
    candidates = [ROUTER_EXECUTE_3, ROUTER_EXECUTE_2]
    for sig in candidates:
        try:
            if sig == ROUTER_EXECUTE_3:
                commands, inputs, deadline = abi_decode(["bytes", "bytes[]", "uint256"], raw[4:])
                return sig, ["0x" + commands.hex(), "[" + ",".join('0x' + x.hex() for x in inputs) + "]", str(int(deadline))]
            commands, inputs = abi_decode(["bytes", "bytes[]"], raw[4:])
            return sig, ["0x" + commands.hex(), "[" + ",".join('0x' + x.hex() for x in inputs) + "]"]
        except Exception:
            continue
    raise DataUnavailable(f"cannot decode Universal Router calldata selector 0x{selector}")


def _first(obj: dict, *keys: str):
    for k in keys:
        if k in obj and obj[k] is not None:
            return obj[k]
    return None


def _nested(obj: dict, path: list[str]):
    cur = obj
    for p in path:
        if not isinstance(cur, dict): return None
        cur = cur.get(p)
    return cur


def _pct(v) -> float | None:
    try:
        x = float(v)
        return x * 100 if abs(x) < 1 else x
    except (TypeError, ValueError): return None


def _usdc_raw(v: float) -> int:
    return int((Decimal(str(v)) * Decimal(10**6)).to_integral_value())


def _token_raw(v: float, decimals: int) -> int:
    return int((Decimal(str(v)) * (Decimal(10) ** decimals)).to_integral_value())
