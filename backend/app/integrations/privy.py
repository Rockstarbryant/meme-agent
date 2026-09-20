"""Minimal Privy REST client for control-plane wallet provisioning.

Signing stays on the shared worker (which holds the same app credentials).
The control plane only creates wallets and stores ids — it never submits trades.
"""
from __future__ import annotations

import base64
from typing import Any

import httpx


class PrivyError(RuntimeError):
    def __init__(self, status: int, detail: Any):
        self.status = status
        self.detail = detail
        super().__init__(f"Privy HTTP {status}: {detail}")


class PrivyAdminClient:
    def __init__(self, app_id: str, app_secret: str, base_url: str = "https://api.privy.io/v1", timeout_s: float = 30.0):
        if not app_id or not app_secret:
            raise ValueError("Privy app_id and app_secret required")
        token = base64.b64encode(f"{app_id}:{app_secret}".encode()).decode()
        self.base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Basic {token}",
            "privy-app-id": app_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._timeout = timeout_s

    async def _request(self, method: str, path: str, *, json_body: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.request(method, f"{self.base_url}{path}", headers=self._headers, json=json_body)
        try:
            body = r.json() if r.content else {}
        except ValueError:
            body = {"raw": (r.text or "")[:2000]}
        if r.status_code >= 400:
            raise PrivyError(r.status_code, body.get("error") or body.get("message") or body)
        return body if isinstance(body, dict) else {"data": body}

    async def create_wallet(self, *, chain_type: str = "ethereum", external_id: str | None = None) -> dict:
        payload: dict = {"chain_type": chain_type}
        if external_id:
            payload["external_id"] = external_id
        return await self._request("POST", "/wallets", json_body=payload)

    async def get_wallet(self, wallet_id: str) -> dict:
        return await self._request("GET", f"/wallets/{wallet_id}")
