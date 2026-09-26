from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import select

from app.api import routes_agent, routes_auth_system, routes_config, routes_platform, routes_runner, routes_stream, routes_trading, routes_wallet
from app.api.deps import Container
from app.chains.arc.adapter import ArcAdapter
from app.chains.evm import EvmRpcClient
from app.launchpads.arc_candidates import arc_candidates
from app.config import Settings
from app.db import models as M
from app.db.session import make_engine, make_session_factory
from app.infra.redis import RateLimiter
from app.services.hub import EventHub
from app.strategies.traction_momentum import TractionMomentumConfig

log = logging.getLogger("arc-agent")


async def seed(sf, settings: Settings) -> None:
    async with sf() as db:
        if await db.get(M.Chain, "arc") is None:
            db.add(M.Chain(id="arc", name="Arc Mainnet", chain_id=settings.arc_chain_id, live_trading_verified=False))
        if await db.get(M.Strategy, "traction_momentum") is None:
            db.add(M.Strategy(id="traction_momentum", name="Traction Momentum",
                              description="Buys only tokens showing measurable, sustained traction; never blind launch sniping."))
            await db.flush()
        for d in arc_candidates():
            if not (await db.execute(select(M.Launchpad).where(M.Launchpad.chain_id == d.chain, M.Launchpad.name == d.name))).first():
                db.add(M.Launchpad(chain_id=d.chain, name=d.name, descriptor=d.model_dump(mode="json"), verified=d.verified, enabled=d.enabled))
        has_default = (await db.execute(select(M.StrategyVersion).where(M.StrategyVersion.strategy_id == "traction_momentum", M.StrategyVersion.user_id.is_(None)))).first()
        if not has_default:
            db.add(M.StrategyVersion(strategy_id="traction_momentum", user_id=None, version=1, config=TractionMomentumConfig().model_dump(mode="json")))
        await db.commit()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.database_url)
        sf = make_session_factory(engine)
        redis = Redis.from_url(settings.redis_url)
        chain = ArcAdapter(EvmRpcClient(settings.arc_rpc_urls), settings.arc_chain_id, settings.arc_explorer_url or "",
                           usdc_address=settings.arc_usdc_address)
        hub = EventHub()
        jwt_key = settings.jwt_secret.get_secret_value()
        if not jwt_key:
            jwt_key = secrets.token_hex(32)
            log.warning("JWT_SECRET not set: using an ephemeral key (tokens die on restart). Set it in production.")
        app.state.c = Container(settings, engine, sf, redis, chain, hub, RateLimiter(redis), jwt_key)
        await seed(sf, settings)
        yield
        await redis.aclose()
        await engine.dispose()

    app = FastAPI(title="Arc Agent API", version="0.3.0", lifespan=lifespan,
                  description="CONTROL PLANE for the Arc trading agent. It never trades, never holds keys or wallet sessions: execution runs on each user's Local Runner. PAPER/LIVE are always labelled; DEMO DATA is never presented as live.")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=False,
                       allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})
        if not request.url.path.startswith("/stream"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"detail": "internal error"}, status_code=500)  # never leak internals

    # Explicit list so a missing/misnamed router fails with a clear message at boot
    # (AttributeError: module has no attribute 'router' usually means a circular import
    # or a deploy that shipped an incomplete routes_*.py).
    route_modules = [
        ("routes_auth_system", routes_auth_system, "router"),
        ("routes_runner", routes_runner, "user_router"),
        ("routes_runner", routes_runner, "runner_router"),
        ("routes_wallet", routes_wallet, "router"),
        ("routes_agent", routes_agent, "router"),
        ("routes_trading", routes_trading, "router"),
        ("routes_config", routes_config, "router"),
        ("routes_stream", routes_stream, "router"),
        ("routes_platform", routes_platform, "router"),
    ]
    for mod_name, mod, attr in route_modules:
        r = getattr(mod, attr, None)
        if r is None:
            raise RuntimeError(
                f"app.api.{mod_name} has no attribute {attr!r}. "
                "Check for a circular import or that the module defines "
                f"{attr} = APIRouter(...)."
            )
        app.include_router(r)
    return app