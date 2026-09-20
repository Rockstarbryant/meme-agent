from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.deps import C, current_user, limit
from app.db import models as M
from app.infra.redis import consume_ticket, issue_ticket

router = APIRouter(tags=["stream"])


@router.post("/stream/ticket")
async def ticket(request: Request, user: M.User = Depends(current_user)):
    """EventSource cannot send headers, so the browser trades its bearer token for a 60s single-use ticket."""
    return {"ticket": await issue_ticket(C(request).redis, user.id), "expires_in": 60}


@router.get("/stream")
async def stream(request: Request, ticket: str):
    await limit(request, "stream", 30, 60)
    c = C(request)
    uid = await consume_ticket(c.redis, ticket)
    if not uid:
        raise HTTPException(401, "invalid or expired ticket")
    q = c.hub.subscribe(uid)

    async def gen():
        try:
            yield f"event: hello\ndata: {json.dumps({'user_id': uid})}\n\n"
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"event: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            c.hub.unsubscribe(uid, q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
