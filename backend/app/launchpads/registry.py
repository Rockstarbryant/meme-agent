from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, model_validator


class LaunchpadDescriptor(BaseModel):
    name: str
    chain: str
    mainnet_live: bool | None = None  # None = unknown
    verified: bool = False            # verified against official docs + on-chain
    enabled: bool = False
    factory_address: str | None = None
    token_creation_event: str | None = None
    router_address: str | None = None
    pool_model: str | None = None     # e.g. "bonding_curve" | "amm"
    graduation_mechanism: str | None = None
    quote_asset: str | None = None
    fees: str | None = None
    liquidity_model: str | None = None
    anti_snipe: str | None = None
    api: str | None = None
    ws_or_indexer: str | None = None
    rpc_requirements: str | None = None
    contract_verified: bool | None = None
    automation_feasible: bool | None = None
    notes: str = ""

    @model_validator(mode="after")
    def _enabled_requires_verified(self) -> "LaunchpadDescriptor":
        if self.enabled and not (self.verified and self.mainnet_live and self.factory_address):
            raise ValueError(f"launchpad '{self.name}' cannot be enabled: needs verified=true, mainnet_live=true, factory_address")
        return self


class LaunchpadRegistry:
    """Ships EMPTY. Entries come from config after verification; nothing is assumed live."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], LaunchpadDescriptor] = {}

    def register(self, d: LaunchpadDescriptor) -> None:
        self._items[(d.chain, d.name)] = d

    def enabled(self, chain: str) -> list[LaunchpadDescriptor]:
        return [d for (c, _), d in self._items.items() if c == chain and d.enabled]

    def all(self) -> list[LaunchpadDescriptor]:
        return list(self._items.values())

    @classmethod
    def from_json(cls, path: str | Path) -> "LaunchpadRegistry":
        reg = cls()
        p = Path(path)
        if p.exists():
            for row in json.loads(p.read_text()):
                reg.register(LaunchpadDescriptor(**row))
        return reg


class LaunchpadAdapter(ABC):
    descriptor: LaunchpadDescriptor

    @abstractmethod
    async def discover_launches(self, since_block: int) -> list[dict]: ...
