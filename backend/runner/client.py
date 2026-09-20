from __future__ import annotations

import httpx

from app.domain.runner_protocol import (CommandAck, ConfigPoll, EventAck, EventBatch, Heartbeat, HeartbeatResponse, PairRequest, PairResponse)


class ControlPlaneError(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail
        super().__init__(f"control plane error {status}: {detail}")


class Revoked(ControlPlaneError):
    """The runner token was revoked: stop taking NEW positions; keep protecting existing ones."""


class ControlPlaneClient:
    """Outbound-only HTTPS client. The runner never listens on a port."""

    def __init__(self, base_url: str, token: str | None = None, http: httpx.AsyncClient | None = None, timeout: float = 15.0):
        self.token = token
        self._http = http or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def _req(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None, timeout: float | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            r = await self._http.request(method, path, json=json, params=params, headers=headers, timeout=timeout)
        except httpx.HTTPError as e:
            raise ControlPlaneError(0, f"unreachable: {type(e).__name__}") from e
        if r.status_code == 401 and self.token:
            raise Revoked(401, "runner token rejected (revoked or invalid)")
        if r.status_code >= 400:
            try:
                detail = str(r.json().get("detail", r.text))
            except ValueError:
                detail = r.text[:200]
            raise ControlPlaneError(r.status_code, detail)
        return r.json()

    async def pair(self, code: str, name: str, version: str) -> PairResponse:
        return PairResponse(**await self._req("POST", "/runner/pair", json=PairRequest(code=code, name=name, version=version).model_dump()))

    async def get_config(self, since: int, wait: int) -> ConfigPoll:
        return ConfigPoll(**await self._req("GET", "/runner/config", params={"since": since, "wait": wait}, timeout=wait + 15))

    async def heartbeat(self, hb: Heartbeat) -> HeartbeatResponse:
        return HeartbeatResponse(**await self._req("POST", "/runner/heartbeat", json=hb.model_dump(mode="json")))

    async def post_events(self, batch: EventBatch) -> EventAck:
        return EventAck(**await self._req("POST", "/runner/events", json=batch.model_dump(mode="json")))

    async def ack_command(self, cid: str, status: str, detail: str = "") -> None:
        await self._req("POST", f"/runner/commands/{cid}/ack", json=CommandAck(status=status, detail=detail[:500]).model_dump())  # type: ignore[arg-type]

    async def aclose(self) -> None:
        await self._http.aclose()
