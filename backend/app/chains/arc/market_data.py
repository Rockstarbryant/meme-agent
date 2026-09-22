from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone

from app.chains.base import MarketDataProvider
from app.core.errors import DataUnavailable
from app.domain.market import ContractInfo, MarketState
from app.integrations.bitquery import BitqueryClient, parse_time


LAUNCHPADS = {
    "argus": {"contracts": ["0xb021be536808f551b31789422fd28a6c9c6e97da"], "sigs": ["1d8917231579f8ce39407f0d616f36f357b07329b0ce5164d0754ac15145ce0a"], "topic_index": 1},
    "radardex": {"contracts": ["0x4b638c1502a07a8e1a26112ee98f51a3f34bc93a", "0x2d933ce4bde6f3d99540b5d7886b383e59b2b2f8"], "sigs": ["851d681a32f0efba577c4a1bd412f74b575764a6b91e499a05a48a23f3821d66"], "topic_index": 1},
    "tolly": {"contracts": ["0xcad7ee36ac193bf2eddb7b3e2736c5bdb8269c8b"], "sigs": ["875522b092d9e19a1de359e4bd218090d582fa521c9733889acf1a5ff1941255"], "topic_index": 1},
    "warp": {"contracts": ["0x0dcad158e98bc24455f9e94f46709d8a5f6d1255"], "sigs": ["0b4cfda446fdf9ec5a85855f088c154869eb62e3e723d7d80319b680f90e0cfd"], "topic_index": 1},
    "archemist": {"contracts": ["0x297cebc4de347347205cd08667b56ee951dd8810"], "sigs": ["8e83c293b82cf6e864a90c1ccffea5e0f1ec23b271eff78e78f1dbd5e32a9c7d"], "topic_index": 1},
    "pegd": {"contracts": ["0xd0aa679ec263e8f9bc929426eb9eab2e061d2c5f"], "sigs": ["4b5a1abdb5ebec3e01fa29e6e1f5e3095f8b3cfaed9ffcd46ad35e7ebc58039e"], "topic_index": 2},
}
CONTRACT_TO_LAUNCHPAD = {c: name for name, v in LAUNCHPADS.items() for c in v["contracts"]}


class UnavailableArcMarketData(MarketDataProvider):
    """Fail-closed provider used when no real Arc market-data source is configured."""
    async def discover_tokens(self) -> list[str]:
        raise DataUnavailable("Arc market data unavailable: configure at least one real Arc market-data provider")

    async def get_market_state(self, token_address: str) -> MarketState:
        raise DataUnavailable("Arc market data unavailable: configure at least one real Arc market-data provider")


class BitqueryArcMarketData(MarketDataProvider):
    """Optional Bitquery Arc provider kept for indexed-data fallback/enrichment."""

    def __init__(self, client: BitqueryClient, *, max_tokens: int = 40, launch_window_hours: int = 24):
        self.client = client
        self.max_tokens = max_tokens
        self.launch_window_hours = launch_window_hours
        self._launch_meta: dict[str, dict] = {}

    async def discover_tokens(self) -> list[str]:
        contracts = [c for v in LAUNCHPADS.values() for c in v["contracts"]]
        sigs = [s for v in LAUNCHPADS.values() for s in v["sigs"]]
        q = """
        query($contracts:[String!]!, $sigs:[String!]!, $hours:Int!) {
          EVM(network:arc) {
            Events(limit:{count:100}, orderBy:{descending:Block_Time}, where:{
              Block:{Time:{since_relative:{hours_ago:$hours}}}
              TransactionStatus:{Success:true}
              Call:{Success:true, Reverted:false}
              LogHeader:{Address:{in:$contracts}, Removed:false}
              Log:{Signature:{SignatureHash:{in:$sigs}}}
            }) {
              Block { Time Number }
              Transaction { Hash From }
              LogHeader { Address }
              Log { Signature { SignatureHash } }
              Topics { Hash }
            }
          }
        }
        """
        data = await self.client.query(q, {"contracts": contracts, "sigs": sigs, "hours": self.launch_window_hours})
        rows = data.get("EVM", {}).get("Events", [])
        seen: list[str] = []
        for row in rows:
            emitter = (row.get("LogHeader", {}).get("Address") or "").lower()
            lp = CONTRACT_TO_LAUNCHPAD.get(emitter)
            sig = ((row.get("Log", {}).get("Signature") or {}).get("SignatureHash") or "").lower()
            cfg = next((v for v in LAUNCHPADS.values() if sig in {s.lower() for s in v["sigs"]}), None)
            if not cfg:
                continue
            topics = row.get("Topics") or []
            hashes = [t.get("Hash") if isinstance(t, dict) else t for t in topics]
            idx = cfg["topic_index"]
            if len(hashes) <= idx or not hashes[idx]:
                continue
            raw = str(hashes[idx])
            token = "0x" + raw[-40:]
            if len(token) != 42 or token.lower() == "0x" + "0" * 40:
                continue
            token = token.lower()
            if token not in seen:
                seen.append(token)
                self._launch_meta[token] = {
                    "launchpad": lp,
                    "creator": (row.get("Transaction") or {}).get("From"),
                    "created_at": parse_time((row.get("Block") or {}).get("Time")),
                    "tx_hash": (row.get("Transaction") or {}).get("Hash"),
                }
            if len(seen) >= self.max_tokens:
                break
        return seen

    async def get_market_state(self, token_address: str) -> MarketState:
        token = token_address.lower()
        tid = f"bid:arc:{token}"
        q = """
        query($tid:String!, $address:String!) {
          Trading {
            Pairs(limit:{count:1}, orderBy:{descending:Block_Time}, where:{
              Token:{Id:{is:$tid}}
              Market:{Network:{is:"Arc"}, ProtocolFamily:{is:"Uniswap"}}
              Interval:{Time:{Duration:{eq:60}}}
              Block:{Time:{since_relative:{hours_ago:24}}}
              Ranking:{Position:{eq:1}}
              Price:{IsQuotedInUsd:true}
            }) {
              Block { Time }
              Token { Id Address Symbol }
              QuoteToken { Id Address Symbol }
              Pool { Address Id }
              Market { Network Protocol }
              Interval { Time { Start End Duration } }
              Price { IsQuotedInUsd Ohlc { Open High Low Close } }
              Ranking { Position Weight }
              Volume { Usd }
            }
            Tokens(limit:{count:20}, orderBy:{descending:Block_Time}, where:{
              Token:{Address:{is:$address}, Network:{is:"Arc"}}
              Interval:{Time:{Duration:{in:[60,300,900]}}}
              Block:{Time:{since_relative:{hours_ago:2}}}
              Price:{IsQuotedInUsd:true}
            }) {
              Block { Time }
              Interval { Time { Start End Duration } }
              Price { Ohlc { Open High Low Close } }
              Supply { MarketCap FullyDilutedValuationUsd CirculatingSupply TotalSupply }
              Volume { Base Usd }
            }
            Trades(limit:{count:200}, orderBy:{descending:Block_Time}, where:{
              Pair:{Market:{Network:{is:"Arc"}, ProtocolFamily:{is:"Uniswap"}}}
              any:[{Pair:{Token:{Id:{is:$tid}}}}, {Pair:{QuoteToken:{Id:{is:$tid}}}}]
              Block:{Time:{since_relative:{minutes_ago:15}}}
            }) {
              Block { Time }
              TransactionHeader { Hash }
              Trader { Address }
              Side
              AmountsInUsd { Quote }
              PriceInUsd
              Pair { Token { Id Address Symbol } QuoteToken { Id Address Symbol } Pool { Address Id } }
            }
          }
          EVM(network:arc) {
            Holders(where:{Currency:{SmartContract:{is:$address}}, Balance:{Amount:{gt:"0"}}}, orderBy:{descending:Balance_Amount}, limit:{count:20}) {
              Holder { Address }
              Balance { Amount }
            }
            HolderCount: Holders(where:{Currency:{SmartContract:{is:$address}}, Balance:{Amount:{gt:"0"}}}) {
              count: uniq(of: Holder_Address)
            }
            DEXPoolEventsA: DEXPoolEvents(limit:{count:5}, orderBy:{descending:Block_Time}, where:{
              PoolEvent:{Pool:{CurrencyA:{SmartContract:{is:$address}}}}
              Block:{Time:{since_relative:{hours_ago:24}}}
            }) {
              Block { Time }
              PoolEvent {
                AtoBPrice BtoAPrice
                Liquidity { AmountCurrencyA AmountCurrencyAInUSD AmountCurrencyB AmountCurrencyBInUSD }
                Pool { SmartContract PoolId CurrencyA { SmartContract } CurrencyB { SmartContract } }
                Dex { ProtocolName }
              }
            }
            DEXPoolEventsB: DEXPoolEvents(limit:{count:5}, orderBy:{descending:Block_Time}, where:{
              PoolEvent:{Pool:{CurrencyB:{SmartContract:{is:$address}}}}
              Block:{Time:{since_relative:{hours_ago:24}}}
            }) {
              Block { Time }
              PoolEvent {
                AtoBPrice BtoAPrice
                Liquidity { AmountCurrencyA AmountCurrencyAInUSD AmountCurrencyB AmountCurrencyBInUSD }
                Pool { SmartContract PoolId CurrencyA { SmartContract } CurrencyB { SmartContract } }
                Dex { ProtocolName }
              }
            }
          }
        }
        """
        data = await self.client.query(q, {"tid": tid, "address": token})
        trading = data.get("Trading", {})
        pairs = trading.get("Pairs") or []
        if not pairs:
            raise DataUnavailable(f"no Arc/Uniswap market found for {token}")
        pair = pairs[0]
        candles = trading.get("Tokens") or []
        trades = trading.get("Trades") or []
        latest = candles[0] if candles else {}
        price_obj = pair.get("Price", {}).get("Ohlc", {})
        price = _num(price_obj.get("Close"))
        market_cap = _num((latest.get("Supply") or {}).get("MarketCap"))
        if price is None:
            price = _num((latest.get("Price") or {}).get("Ohlc", {}).get("Close"))
        now = datetime.now(timezone.utc)
        buy5 = sell5 = vol1 = vol5 = vol15 = 0.0
        buys1 = sells1 = buys5 = sells5 = 0
        ub1: set[str] = set(); ub5: set[str] = set(); ub15: set[str] = set()
        us1: set[str] = set(); us5: set[str] = set(); us15: set[str] = set()
        recent_prices: list[tuple[datetime,float]] = []
        seen_hashes: set[str] = set()
        for t in trades:
            ts = parse_time((t.get("Block") or {}).get("Time"))
            if not ts:
                continue
            age = (now - ts).total_seconds()
            if age < 0 or age > 900:
                continue
            h = ((t.get("TransactionHeader") or {}).get("Hash") or "")
            # Do not double-count an identical indexed trade row.
            key = f"{h}:{(t.get('Pair') or {}).get('Pool',{}).get('Id','')}:{ts.isoformat()}"
            if key in seen_hashes:
                continue
            seen_hashes.add(key)
            usd = _num((t.get("AmountsInUsd") or {}).get("Quote")) or 0.0
            side = str(t.get("Side") or "").lower()
            trader = ((t.get("Trader") or {}).get("Address") or "").lower()
            p = _num(t.get("PriceInUsd"))
            if p:
                recent_prices.append((ts,p))
            if age <= 60:
                vol1 += usd
                if side == "buy": buys1 += 1; ub1.add(trader); buy5 += usd; buys5 += 1; ub5.add(trader)
                elif side == "sell": sells1 += 1; us1.add(trader); sell5 += usd; sells5 += 1; us5.add(trader)
            elif age <= 300:
                vol5 += usd
                if side == "buy": buy5 += usd; buys5 += 1; ub5.add(trader)
                elif side == "sell": sell5 += usd; sells5 += 1; us5.add(trader)
            else:
                vol15 += usd
                if side == "buy": ub15.add(trader)
                elif side == "sell": us15.add(trader)
        # Convert 1m/5m/15m buckets into cumulative windows.
        vol5 += vol1; vol15 += vol5
        ub15.update(ub5); us15.update(us5)
        evm = data.get("EVM", {})
        holders = (evm.get("Holders") or [])
        hc_rows = evm.get("HolderCount") or []
        holder_count = _num((hc_rows[0] if hc_rows else {}).get("count")) if hc_rows else None
        holder_count = int(holder_count) if holder_count is not None else None
        top20 = [_num((h.get("Balance") or {}).get("Amount")) or 0 for h in holders]
        total_supply = _num((latest.get("Supply") or {}).get("TotalSupply"))
        top10_pct = (sum(top20[:10]) / total_supply * 100) if total_supply and total_supply > 0 else None
        top5_pct = (sum(top20[:5]) / total_supply * 100) if total_supply and total_supply > 0 else None
        top20_pct = (sum(top20[:20]) / total_supply * 100) if total_supply and total_supply > 0 else None
        liquidity_rows = (evm.get("DEXPoolEventsA") or []) + (evm.get("DEXPoolEventsB") or [])
        liquidity = _pool_liquidity(liquidity_rows, token, pair.get("Pool",{}).get("Address"), pair.get("Pool",{}).get("Id"))
        meta = self._launch_meta.get(token, {})
        created = meta.get("created_at")
        recent_high = max([p for _, p in recent_prices], default=price)
        p1 = _candle_change(candles, 60)
        p5 = _candle_change(candles, 300)
        p15 = _candle_change(candles, 900)
        contract = ContractInfo(verified=None, sell_simulation_ok=None)
        return MarketState(
            chain="arc", token_address=token, timestamp=now, launchpad=meta.get("launchpad"),
            pool_address=(pair.get("Pool") or {}).get("Address"), pool_id=(pair.get("Pool") or {}).get("Id"),
            symbol=((pair.get("Token") or {}).get("Symbol") or (latest.get("Token") or {}).get("Symbol")),
            creator_address=meta.get("creator"), token_created_at=created,
            price=price, market_cap=market_cap, liquidity=liquidity,
            volume_1m=vol1 or None, volume_5m=vol5 or None, volume_15m=vol15 or None,
            buy_volume_5m=buy5 or None, sell_volume_5m=sell5 or None,
            buys_1m=buys1 or None, sells_1m=sells1 or None, buys_5m=buys5 or None, sells_5m=sells5 or None,
            unique_buyers_1m=len(ub1) or None, unique_buyers_5m=len(ub5) or None, unique_buyers_15m=len(ub15) or None,
            unique_sellers_1m=len(us1) or None, unique_sellers_5m=len(us5) or None, unique_sellers_15m=len(us15) or None,
            holder_count=holder_count, top5_holder_pct=top5_pct, top10_holder_pct=top10_pct, top20_holder_pct=top20_pct,
            creator_known=bool(meta.get("creator")), creator_balance_pct=None, creator_sold_pct=None,
            price_change_1m=p1, price_change_5m=p5, price_change_15m=p15, recent_high=recent_high,
            contract=contract, data_sources=["bitquery:trading", "bitquery:holders", "bitquery:dexpools"], is_demo=False,
        )

    async def aclose(self) -> None:
        await self.client.close()


def _num(v) -> float | None:
    if v is None or v == "": return None
    try: return float(v)
    except (TypeError, ValueError): return None


def _candle_change(candles: list[dict], duration: int) -> float | None:
    rows = [r for r in candles if ((r.get("Interval") or {}).get("Time") or {}).get("Duration") == duration]
    if not rows: return None
    rows.sort(key=lambda r: str(((r.get("Interval") or {}).get("Time") or {}).get("End") or ""))
    o = _num(((rows[0].get("Price") or {}).get("Ohlc") or {}).get("Open"))
    c = _num(((rows[-1].get("Price") or {}).get("Ohlc") or {}).get("Close"))
    return ((c/o)-1)*100 if o and c else None


def _pool_liquidity(rows: list[dict], token: str, pool_addr: str | None, pool_id: str | None) -> float | None:
    candidates = []
    for row in rows:
        pe = row.get("PoolEvent") or {}; pool = pe.get("Pool") or {}
        if pool_addr and (pool.get("SmartContract") or "").lower() != pool_addr.lower():
            continue
        if pool_id and (pool.get("PoolId") or "").lower() not in {pool_id.lower(), ""}:
            continue
        liq = pe.get("Liquidity") or {}
        a = _num(liq.get("AmountCurrencyAInUSD")) or 0.0
        b = _num(liq.get("AmountCurrencyBInUSD")) or 0.0
        candidates.append(a+b)
    return max(candidates) if candidates else None
