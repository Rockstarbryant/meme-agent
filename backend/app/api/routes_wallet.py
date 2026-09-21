from __future__ import annotations

import secrets
from datetime import timedelta

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import C, current_user, get_db, limit
from app.services import control
from app.chains.arc.network import NETWORKS, wallet_chain_params
from app.core.clock import utcnow
from app.core.types import WalletCapability
from app.db import models as M
from app.db import repo
from app.wallets.base import WalletPolicy
from app.wallets.providers import AgentWalletProvider

router = APIRouter(prefix="/wallet", tags=["wallet"])
AGENT_WALLET_REASON = (
    "Autonomous execution runs on YOUR Local Runner, using your own Circle Agent Wallet session on your machine; this server never "
    "holds keys or sessions. It stays disabled until Circle CLI output schemas and the Arc DEX execution path are verified. See docs/local-runner.md.")
AGENT_WALLET = {
    "available": False, "product": "Circle Agent Wallet (Circle CLI, @circle-fin/cli), operated by your Local Runner", "reason": AGENT_WALLET_REASON,
    "documented": ["non-custodial agent-controlled smart-contract wallets; Arc is the default chain",
                   "`circle wallet execute <abi signature> <params...> --contract --address --chain [--estimate] [--idempotency-key]`",
                   "spending caps per-tx / daily / weekly / monthly in USDC (`wallet limit`, `wallet limit budget`); set/reset need a human OTP; mainnet only",
                   "`circle wallet status` reports mainnet and testnet sessions independently; login is email + OTP by the human"],
    "not_documented": ["JSON output schemas of status / execute / transaction list (cannot be seen without an authenticated session)",
                       "how an agent-wallet transaction id maps to a chain transaction hash (`circle transaction` has list/cancel/accelerate only)",
                       "whether contract writes (swaps) count toward the USDC caps", "session lifetime for unattended use",
                       "contract/router allowlists and slippage limits (enforced by this app instead)"],
    "constraints": ["spending policies are mainnet-only", "OTP entry stays with the human: neither this server nor the runner ever sees it"],
    "decision_doc": "docs/local-runner.md"}
WALLET_OPTIONS = [
    {"id": "browser_wallet", "label": "Browser wallet (EIP-1193), per-trade signing", "status": "available", "custody": "user",
     "note": "Optional manual signing. Not used for autonomous mode (it cannot run unattended)."},
    {"id": "circle_agent_wallet", "label": "Circle Agent Wallet via Local Runner", "status": "not_integrated", "custody": "user (agent-controlled SCA, on your machine)",
     "note": "Adapter implemented from the CLI --help; LIVE stays disabled until output schemas and the Arc execution path are verified."},
    {"id": "circle_modular_wallet", "label": "Circle Modular Wallet (passkeys)", "status": "not_integrated", "custody": "user",
     "note": "Supports Arc (frontend SDK, gasless). No documented delegation/session-key primitive for unattended agents."},
    {"id": "circle_user_controlled_wallet", "label": "Circle User-Controlled Wallet", "status": "not_integrated", "custody": "user",
     "note": "Every sensitive operation needs the user's PIN/OTP approval: per-trade by design."},
    {"id": "circle_developer_controlled_wallet", "label": "Circle Developer-Controlled Wallet", "status": "forbidden", "custody": "developer (custodial)",
     "note": "Server-side custody. Rejected by this project's non-custodial requirement."},
    {"id": "privy_cloud", "label": "Privy managed wallet (cloud agent)", "status": "available", "custody": "platform app-scoped (Privy TEE)",
     "note": "Per-user server wallet; shared worker signs within your policy. See docs/privy-wallet.md."},
]


class Challenge(BaseModel):
    address: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")


class Connect(Challenge):
    signature: str = Field(min_length=100, max_length=200)


class PolicyIn(BaseModel):
    allocated_capital_usdc: float = Field(gt=0, le=10_000_000)
    max_trade_usdc: float = Field(gt=0)
    max_position_usdc: float = Field(gt=0)
    max_daily_loss_usdc: float = Field(gt=0)
    max_open_positions: int = Field(ge=1, le=50)
    max_slippage_pct: float = Field(gt=0, le=20)
    min_liquidity_usdc: float = Field(ge=0)
    allowed_chains: set[str] = {"arc"}
    allowed_launchpads: set[str] = set()
    allowed_routers: set[str] = set()
    confirm: bool = False

    @model_validator(mode="after")
    def _sane(self):
        if not (self.max_trade_usdc <= self.max_position_usdc <= self.allocated_capital_usdc):
            raise ValueError("require max_trade <= max_position <= allocated_capital")
        return self


class AuthorizeIn(BaseModel):
    capability: WalletCapability
    expires_in_hours: int = Field(24, ge=1, le=24 * 30)


class SignedIn(BaseModel):
    tx_hash: str = Field(pattern=r"^0x[0-9a-fA-F]{64}$")


async def _wallet(db: AsyncSession, user_id: str, create_paper: bool = False) -> M.Wallet | None:
    w = (await db.execute(select(M.Wallet).where(M.Wallet.user_id == user_id).order_by(M.Wallet.ownership_verified.desc(), M.Wallet.created_at.desc()))).scalars().first()
    if w is None and create_paper:
        w = M.Wallet(user_id=user_id, provider="paper", kind="paper", address="PAPER", chain="arc", ownership_verified=False)
        db.add(w)
        await db.flush()
    return w


async def _policy(db: AsyncSession, wallet_id: str) -> M.WalletPolicyRow | None:
    return (await db.execute(select(M.WalletPolicyRow).where(M.WalletPolicyRow.wallet_id == wallet_id, M.WalletPolicyRow.is_current.is_(True)))).scalars().first()


async def _active_auth(db: AsyncSession, wallet_id: str) -> M.WalletAuthorizationRow | None:
    rows = (await db.execute(select(M.WalletAuthorizationRow).where(M.WalletAuthorizationRow.wallet_id == wallet_id, M.WalletAuthorizationRow.revoked_at.is_(None)))).scalars().all()
    now = utcnow()
    return next((a for a in rows if not a.expires_at or a.expires_at > now), None)


@router.get("")
async def wallet_state(request: Request, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    c = C(request)
    wallets = (await db.execute(select(M.Wallet).where(M.Wallet.user_id == user.id))).scalars().all()
    w = await _wallet(db, user.id)
    pol = await _policy(db, w.id) if w else None
    auth = await _active_auth(db, w.id) if w else None
    verified = w is not None and w.ownership_verified
    if verified and auth and auth.capability == WalletCapability.PER_TRADE_SIGNING.value:
        cap = {"capability": "PER_TRADE_SIGNING", "label": "Explicit per-trade signing", "autonomous": False,
               "detail": "Every live trade needs your wallet signature."}
    else:
        cap = {"capability": "PAPER_ONLY", "label": "Paper trading only", "autonomous": False,
               "detail": "Connect a wallet, set a policy and authorize per-trade signing to enable live signing."}
    usdc = {"balance": None, "view": "ERC-20, 6 decimals", "address": c.settings.arc_usdc_address,
            "note": "Connect a wallet to read the balance. The native 18-decimal view is the same funds and is never added."}
    if verified:
        try:
            usdc = {**usdc, "balance": await c.chain.usdc_balance(w.address), "note": "ERC-20 (6-decimal) view of the single USDC balance"}
        except Exception as e:  # noqa: BLE001
            usdc = {**usdc, "note": f"balance unavailable: {type(e).__name__}"}
    pf = (await db.execute(select(M.Portfolio).where(M.Portfolio.user_id == user.id, M.Portfolio.mode == user.mode))).scalar_one_or_none()
    runner = await control.active_runner(db, user.id)
    rst = (runner.status or {}) if runner else {}
    cfg = await control.get_config(db, user.id)
    cloud = next((x for x in wallets if x.provider == "privy" and x.external_id), None)
    if cloud is not None and getattr(cfg, "execution_mode", "self_hosted") == "cloud_managed":
        cap = {"capability": "AUTONOMOUS_DELEGATED", "label": "Autonomous cloud execution", "autonomous": True,
               "detail": "The shared worker can execute through your per-user Privy managed wallet, subject to the configured policy and LIVE gates."}
    return {
        "network": {"chain": "arc", "network": c.settings.arc_network, "chain_id": c.settings.arc_chain_id,
                    "explorer_url": c.settings.arc_explorer_url, "health": (await c.chain.health()).model_dump(),
                    "chain_params": wallet_chain_params(NETWORKS[c.settings.arc_network])},
        "wallets": [{"id": x.id, "provider": x.provider, "address": x.address, "ownership_verified": x.ownership_verified} for x in wallets],
        "agent_wallet": AGENT_WALLET, "wallet_options": WALLET_OPTIONS,
        "cloud_wallet": None if cloud is None else {"id": cloud.id, "address": cloud.address, "privy_wallet_id": cloud.external_id, "active": getattr(cfg, "execution_mode", "self_hosted") == "cloud_managed"},
        "execution_mode": getattr(cfg, "execution_mode", "self_hosted"),
        "execution_capability": cap,
        "authorization": None if not auth else {"id": auth.id, "capability": auth.capability, "granted_at": auth.granted_at, "expires_at": auth.expires_at},
        "policy": None if not pol else {"version": pol.version, **pol.policy},
        "usdc": usdc,
        "allocated_capital_usdc": pol.policy["allocated_capital_usdc"] if pol else None,
        "available_trading_capital_usdc": pf.cash_usdc if pf else None,
        "runner_wallet": None if runner is None else {"provider": rst.get("wallet_provider"), "online": control.is_online(runner), "live": rst.get("live")},
        "capital_label": "PAPER (virtual USDC)" if user.mode == "PAPER" else "LIVE",
        "mode": user.mode,
        "live_blockers": await control.live_blockers(db, c.settings, user.id),
    }


@router.post("/challenge")
async def challenge(body: Challenge, request: Request, user: M.User = Depends(current_user)):
    await limit(request, "wchal", 10, 60, extra=user.id)
    nonce = secrets.token_hex(16)
    msg = f"Link wallet {body.address.lower()} to Arc Agent account {user.id}. Nonce: {nonce}"
    await C(request).redis.set(f"wchal:{user.id}:{body.address.lower()}", msg, ex=300)
    return {"message": msg, "expires_in": 300}


@router.post("/connect", status_code=201)
async def connect(body: Connect, request: Request, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await limit(request, "wconnect", 10, 60, extra=user.id)
    r = C(request).redis
    key = f"wchal:{user.id}:{body.address.lower()}"
    msg = await r.getdel(key)
    if not msg:
        raise HTTPException(400, "no pending challenge (request /wallet/challenge first)")
    try:
        recovered = Account.recover_message(encode_defunct(text=msg.decode() if isinstance(msg, bytes) else msg), signature=body.signature)
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "invalid signature")
    if recovered.lower() != body.address.lower():
        raise HTTPException(400, "signature does not match address")
    existing = (await db.execute(select(M.Wallet).where(M.Wallet.user_id == user.id, M.Wallet.chain == "arc", M.Wallet.address == body.address.lower()))).scalar_one_or_none()
    w = existing or M.Wallet(user_id=user.id, provider="browser_wallet", kind="browser", address=body.address.lower(), chain="arc")
    w.ownership_verified = True
    db.add(w)
    await repo.audit(db, user.id, user.email, "WALLET_CONNECTED", address=body.address.lower())
    await db.commit()
    return {"id": w.id, "address": w.address, "ownership_verified": True, "provider": w.provider}


@router.post("/policy")
async def set_policy(body: PolicyIn, request: Request, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if user.mode == "LIVE" and not body.confirm:
        raise HTTPException(409, "changing the wallet policy in LIVE mode requires confirm=true")
    w = await _wallet(db, user.id, create_paper=True)
    prev = await _policy(db, w.id)
    version = (prev.version + 1) if prev else 1
    data = WalletPolicy(**body.model_dump(exclude={"confirm"})).model_dump(mode="json")
    await db.execute(update(M.WalletPolicyRow).where(M.WalletPolicyRow.wallet_id == w.id).values(is_current=False))
    row = M.WalletPolicyRow(wallet_id=w.id, version=version, policy=data)
    db.add(row)
    # any policy change invalidates existing authorizations: the user must re-authorize under the new policy
    await db.execute(update(M.WalletAuthorizationRow).where(M.WalletAuthorizationRow.wallet_id == w.id, M.WalletAuthorizationRow.revoked_at.is_(None)).values(revoked_at=utcnow()))
    await repo.audit(db, user.id, user.email, "WALLET_POLICY_SET", version=version, before=prev.policy if prev else None, after=data)
    await control.bump(db, user.id)
    await db.commit()
    return {"version": version, "policy": data, "note": "Delivered to your Local Runner. Existing manual-signing authorizations were revoked."}


@router.post("/authorize", status_code=201)
async def authorize(body: AuthorizeIn, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if body.capability == WalletCapability.AUTONOMOUS_DELEGATED:
        AgentWalletProvider()  # documents the disabled provider
        raise HTTPException(501, {"error": "INTEGRATION_NOT_VERIFIED", "detail": AGENT_WALLET_REASON,
                                  "requires": "custody-model decision (docs/circle-agent-wallet.md)"})
    if body.capability != WalletCapability.PER_TRADE_SIGNING:
        raise HTTPException(422, "unsupported capability")
    w = await _wallet(db, user.id)
    if w is None or not w.ownership_verified:
        raise HTTPException(409, "connect a wallet and prove ownership first")
    pol = await _policy(db, w.id)
    if pol is None:
        raise HTTPException(409, "configure a wallet policy first")
    a = M.WalletAuthorizationRow(wallet_id=w.id, capability=body.capability.value, policy_id=pol.id, expires_at=utcnow() + timedelta(hours=body.expires_in_hours))
    db.add(a)
    await repo.audit(db, user.id, user.email, "WALLET_AUTHORIZED", capability=body.capability.value, policy_version=pol.version)
    await db.commit()
    return {"id": a.id, "capability": a.capability, "expires_at": a.expires_at,
            "meaning": "The agent may queue trades within the policy; each one still requires your wallet signature. The backend never holds your keys."}


@router.post("/revoke")
async def revoke(request: Request, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    w = await _wallet(db, user.id)
    n = 0
    if w:
        res = await db.execute(update(M.WalletAuthorizationRow).where(M.WalletAuthorizationRow.wallet_id == w.id, M.WalletAuthorizationRow.revoked_at.is_(None)).values(revoked_at=utcnow()))
        n = res.rowcount
    switched = False
    if user.mode == "LIVE":
        from sqlalchemy import func
        open_n = (await db.execute(select(func.count()).select_from(M.Position).where(M.Position.user_id == user.id, M.Position.mode == "LIVE", M.Position.status == "OPEN"))).scalar_one()
        if open_n == 0:
            user.mode, switched = "PAPER", True
            db.add(user)
    await control.bump(db, user.id, desired_state="STOPPED")
    await repo.audit(db, user.id, user.email, "WALLET_REVOKED", revoked=n, switched_to_paper=switched)
    await db.commit()
    return {"revoked": n, "switched_to_paper": switched,
            "note": "Withdrawals and on-chain approvals are controlled in your own wallet; revoke any token approvals there as well."}


@router.get("/signing-requests")
async def signing_requests(user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(M.SigningRequestRow).where(M.SigningRequestRow.user_id == user.id).order_by(M.SigningRequestRow.created_at.desc()).limit(50))).scalars().all()
    return [{"id": r.id, "status": r.status, "tx": r.tx, "tx_hash": r.tx_hash, "created_at": r.created_at} for r in rows]


@router.post("/signing-requests/{rid}/complete")
async def complete_signing(rid: str, body: SignedIn, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    r = await db.get(M.SigningRequestRow, rid)
    if r is None or r.user_id != user.id:
        raise HTTPException(404, "not found")
    if r.status != "PENDING":
        raise HTTPException(409, f"already {r.status}")
    r.status, r.tx_hash = "SIGNED", body.tx_hash
    await repo.audit(db, user.id, user.email, "SIGNING_COMPLETED", request_id=rid, tx_hash=body.tx_hash)
    await db.commit()
    return {"id": r.id, "status": r.status, "note": "Confirmation is verified on-chain by the agent before the portfolio is updated."}


@router.get("/activity")
async def wallet_activity(user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(M.AuditLog).where(M.AuditLog.user_id == user.id, M.AuditLog.action.like("WALLET_%")).order_by(M.AuditLog.at.desc()).limit(100))).scalars().all()
    return [{"at": r.at, "action": r.action, "detail": r.detail} for r in rows]


class CloudProvisionIn(BaseModel):
    confirm: bool = False


@router.post("/cloud/provision", status_code=201)
async def provision_cloud_wallet(
    body: CloudProvisionIn,
    request: Request,
    user: M.User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a per-user Privy server wallet and switch the account to cloud_managed execution."""
    settings = C(request).settings
    if not settings.cloud_managed_enabled:
        raise HTTPException(403, "Cloud managed wallets are disabled (CLOUD_MANAGED_ENABLED=false)")
    if not settings.privy_app_id or not settings.privy_app_secret:
        raise HTTPException(503, "Privy is not configured on the control plane")
    if not body.confirm:
        raise HTTPException(409, "confirm=true required — you accept platform-scoped signing within your policy")

    existing = (
        await db.execute(
            select(M.Wallet).where(M.Wallet.user_id == user.id, M.Wallet.provider == "privy", M.Wallet.external_id.is_not(None))
        )
    ).scalars().first()
    if existing:
        await control.bump(db, user.id, execution_mode="cloud_managed")
        await db.commit()
        return {
            "id": existing.id,
            "address": existing.address,
            "privy_wallet_id": existing.external_id,
            "provider": "privy",
            "execution_mode": "cloud_managed",
            "note": "Existing cloud wallet re-enabled. Fund this address with USDC on Arc before LIVE.",
        }

    from app.integrations.privy import PrivyAdminClient, PrivyError

    client = PrivyAdminClient(
        settings.privy_app_id,
        settings.privy_app_secret.get_secret_value(),
        base_url=settings.privy_api_url,
    )
    try:
        created = await client.create_wallet(chain_type="ethereum", external_id=f"user-{user.id}")
    except PrivyError as e:
        raise HTTPException(502, f"Privy wallet create failed: {e.detail}") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Privy unreachable: {type(e).__name__}") from e

    privy_id = str(created.get("id") or "")
    address = str(created.get("address") or "").lower()
    if not privy_id or not address:
        raise HTTPException(502, f"Privy response missing id/address: {created}")

    w = M.Wallet(
        user_id=user.id,
        provider="privy",
        kind="agent",
        address=address,
        chain="arc",
        ownership_verified=True,
        external_id=privy_id,
    )
    db.add(w)
    await db.flush()
    # Default conservative policy if none exists
    if await _policy(db, w.id) is None:
        from app.wallets.base import WalletPolicy
        from app.chains.arc.deployments import ARC_UNIVERSAL_ROUTER

        routers: set[str] = {ARC_UNIVERSAL_ROUTER.lower()}
        pol = WalletPolicy(
            allocated_capital_usdc=100,
            max_trade_usdc=min(25.0, settings.risk_max_trade_usdc),
            max_position_usdc=min(50.0, settings.risk_max_position_usdc),
            max_daily_loss_usdc=min(50.0, settings.risk_max_daily_loss_usdc),
            max_open_positions=min(5, settings.risk_max_open_positions),
            max_slippage_pct=min(2.0, settings.risk_max_slippage_pct),
            min_liquidity_usdc=settings.risk_min_liquidity_usdc,
            allowed_chains={"arc"},
            allowed_routers=routers,
        )
        db.add(M.WalletPolicyRow(wallet_id=w.id, version=1, policy=pol.model_dump(mode="json"), is_current=True))

    await control.bump(db, user.id, execution_mode="cloud_managed")
    await repo.audit(db, user.id, user.email, "CLOUD_WALLET_PROVISIONED", privy_wallet_id=privy_id, address=address)
    await db.commit()
    return {
        "id": w.id,
        "address": address,
        "privy_wallet_id": privy_id,
        "provider": "privy",
        "execution_mode": "cloud_managed",
        "note": "Fund this address with USDC on Arc. The shared worker will trade only within your risk limits and wallet policy.",
    }


@router.post("/cloud/disable")
async def disable_cloud_wallet(user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Stop cloud execution for this user (wallet remains; re-enable via provision)."""
    await control.bump(db, user.id, execution_mode="self_hosted", desired_state="STOPPED")
    if user.mode == "LIVE":
        user.mode = "PAPER"
        db.add(user)
    await repo.audit(db, user.id, user.email, "CLOUD_WALLET_DISABLED")
    await db.commit()
    return {"execution_mode": "self_hosted", "mode": user.mode}
