from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.errors import DataUnavailable

BITQUERY_HTTP = "https://streaming.bitquery.io/graphql"


class BitqueryClient:
    """Small async Bitquery V2 client. No subscriptions are hidden behind this class; HTTP is used for deterministic snapshots."""

    def __init__(self, api_key: str, endpoint: str = BITQUERY_HTTP, timeout_s: float = 15.0):
        if not api_key:
            raise ValueError("BITQUERY_API_KEY is required for real Arc market data")
        self.api_key = api_key
        self.endpoint = endpoint
        self.client = httpx.AsyncClient(timeout=timeout_s, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    async def query(self, document: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = await self.client.post(self.endpoint, json={"query": document, "variables": variables or {}})
                r.raise_for_status()
                body = r.json()
                if body.get("errors"):
                    raise DataUnavailable("Bitquery GraphQL error: " + str(body["errors"])[:1000])
                return body.get("data") or {}
            except (httpx.HTTPError, ValueError, DataUnavailable) as exc:
                last = exc
                if attempt < 2:
                    await asyncio.sleep(0.35 * (attempt + 1))
        raise DataUnavailable(f"Bitquery request failed: {type(last).__name__}: {last}")

    async def close(self) -> None:
        await self.client.aclose()


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
