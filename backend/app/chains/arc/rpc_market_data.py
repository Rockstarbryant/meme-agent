from __future__ import annotations

from datetime import datetime, timezone

from app.chains.base import MarketDataProvider
from app.chains.arc.market_data import CONTRACT_TO_LAUNCHPAD, LAUNCHPADS
from app.chains.evm import EvmRpcClient
from app.core.errors import DataUnavailable
from app.domain.market import ContractInfo, MarketState

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628c55a2a5c8f7d0"


def _hex_int(value: str | None) -> int:
    return int(value or "0x0", 16)


def _address_topic(topic: str) -> str | None:
    raw = str(topic or "")
    if len(raw) < 40:
        return None
    return "0x" + raw[-40:].lower()


def _decode_text(raw: str | None) -> str | None:
    if not raw or raw == "0x":
        return None
    try:
        b = bytes.fromhex(raw[2:] if raw.startswith("0x") else raw)
        if len(b) >= 64:
            offset = int.from_bytes(b[:32], "big")
            if offset + 32 <= len(b):
                n = int.from_bytes(b[offset:offset + 32], "big")
                start = offset + 32
                return b[start:start + n].decode("utf-8", errors="ignore").strip("\x00") or None
        return b.decode("utf-8", errors="ignore").strip("\x00\x20") or None
    except Exception:
        return None


class ArcRpcMarketData(MarketDataProvider):
    """Free Arc-native discovery/metadata provider.

    It uses only Arc JSON-RPC. Market price/liquidity/flow enrichment is left to
    another provider (normally GeckoTerminal) because Arc is currently Uniswap-v4
    centric and reproducing a complete indexed DEX dataset from raw logs is a
    separate indexing job. Unknown market fields remain None; nothing is guessed.
    """

    def __init__(self, rpc: EvmRpcClient, *, max_tokens: int = 20, scan_blocks: int = 43200, cache_s: float = 60.0):
        self.rpc = rpc
        self.max_tokens = max(1, max_tokens)
        self.scan_blocks = max(100, scan_blocks)
        self.cache_s = max(5.0, cache_s)
        self._launch_meta: dict[str, dict] = {}
        self._cached_tokens: list[str] = []
        self._last_discovery = 0.0

    async def discover_tokens(self) -> list[str]:
        import time
        if self._cached_tokens and time.monotonic() - self._last_discovery < self.cache_s:
            return self._cached_tokens[: self.max_tokens]
        latest = _hex_int(await self.rpc.call("eth_blockNumber"))
        start = max(0, latest - self.scan_blocks)
        seen: list[str] = []
        groups: list[tuple[str, str, int, str]] = []
        for name, cfg in LAUNCHPADS.items():
            for contract in cfg["contracts"]:
                for sig in cfg["sigs"]:
                    groups.append((contract, sig, int(cfg["topic_index"]), name))
        any_success = False
        for contract, sig, topic_index, launchpad in groups:
            try:
                # Route through EvmRpcClient.get_logs() so a "range too large"
                # refusal is narrowed automatically instead of being retried
                # identically and 400'ing every cycle (the Alchemy-on-Arc bug).
                rows = await self.rpc.get_logs(
                    address=contract,
                    topics=[sig if sig.startswith("0x") else "0x" + sig],
                    from_block=start,
                    to_block=latest,
                ) or []
                any_success = True
            except Exception as exc:
                # One broken launchpad should not disable all other venues.
                continue
            for row in rows:
                topics = row.get("topics") or []
                if len(topics) <= topic_index:
                    continue
                token = _address_topic(topics[topic_index])
                if not token or token == "0x" + "0" * 40:
                    continue
                if token not in self._launch_meta:
                    tx_hash = row.get("transactionHash")
                    block_no = _hex_int(row.get("blockNumber"))
                    creator = None
                    created_at = None
                    try:
                        tx = await self.rpc.call("eth_getTransactionByHash", [tx_hash]) if tx_hash else None
                        creator = (tx or {}).get("from")
                        block = await self.rpc.call("eth_getBlockByNumber", [hex(block_no), False])
                        ts = _hex_int((block or {}).get("timestamp"))
                        created_at = datetime.fromtimestamp(ts, timezone.utc) if ts else None
                    except Exception:
                        pass
                    self._launch_meta[token] = {"launchpad": launchpad, "creator": creator, "created_at": created_at, "tx_hash": tx_hash}
                if token not in seen:
                    seen.append(token)
                if len(seen) >= self.max_tokens:
                    self._cached_tokens = seen
                    self._last_discovery = time.monotonic()
                    return seen
        if not any_success:
            raise DataUnavailable("Arc RPC launchpad log queries failed")
        self._cached_tokens = seen
        self._last_discovery = time.monotonic()
        return seen

    async def get_market_state(self, token_address: str) -> MarketState:
        token = token_address.lower()
        meta = self._launch_meta.get(token, {})
        symbol = name = None
        total_supply = None
        try:
            symbol = _decode_text(await self.rpc.call("eth_call", [{"to": token, "data": "0x95d89b41"}, "latest"]))
        except Exception:
            pass
        try:
            name = _decode_text(await self.rpc.call("eth_call", [{"to": token, "data": "0x06fdde03"}, "latest"]))
        except Exception:
            pass
        try:
            raw_supply = await self.rpc.call("eth_call", [{"to": token, "data": "0x18160ddd"}, "latest"])
            total_supply = float(_hex_int(raw_supply))
        except Exception:
            pass
        now = datetime.now(timezone.utc)
        return MarketState(
            chain="arc", token_address=token, timestamp=now,
            launchpad=meta.get("launchpad"), symbol=symbol or name,
            creator_address=meta.get("creator"), creator_known=bool(meta.get("creator")),
            token_created_at=meta.get("created_at"),
            contract=ContractInfo(verified=None),
            data_sources=["arc_rpc:launchpad_events", "arc_rpc:erc20_metadata"], is_demo=False,
        )

    async def aclose(self) -> None:
        # Shared EvmRpcClient ownership belongs to RunnerRuntime.
        return None