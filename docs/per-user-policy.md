# Per-user policy enforcement

Shared-worker / multi-tenant trading requires **isolation**: user A’s limits, wallet, and portfolio must never authorize a trade for user B.

## Layers (strictest wins)

```
platform ceilings  ∩  user risk limits  ∩  wallet policy  ∩  local runner ceilings
```

| Layer | Owned by | Purpose |
|-------|----------|---------|
| **Platform ceilings** | Operator (`PLATFORM_MAX_*` env on API) | Hard cap no user can exceed |
| **User risk limits** | User (Settings → Risk) | Per-account preferences |
| **Wallet policy** | User / provisioning | Capital allocation, routers, functions |
| **Local ceilings** | Runner host | Machine-level safety (self-hosted) |

## What was implemented

1. **`app/risk/per_user_policy.py`**
   - `PlatformCeilings`, `UserPolicyContext`
   - `clamp_to_platform`, `effective_limits`, `reject_if_above_platform`
   - `PerUserPolicyEnforcer.authorize_trade` — binding + pause + amount checks
   - `scoped_idempotency_key(user_id, raw)` — no cross-user idempotency collisions

2. **`WalletAuthorization`** optional fields: `user_id`, `wallet_address`, `policy_version`

3. **`PolicyValidator`** accepts `expected_user_id` / `expected_wallet_id` / `expected_wallet_address`

4. **`ConfigBundle`** carries `user_id`, wallet binding, `platform_ceilings` to the worker

5. **Control plane**
   - `load_limits` always clamps to platform ceilings
   - `build_bundle` ships effective limits + user/wallet identity
   - `PUT /risk/limits` rejects values above platform ceilings (HTTP 422)

6. **Runner**
   - Portfolio state keyed by `portfolio:{user_id}:{mode}` when `user_id` is present
   - Re-applies platform + local ceilings on every config apply

## API behaviour

```http
GET /risk/limits
→ limits, effective_limits, platform_ceilings

PUT /risk/limits
→ 422 EXCEEDS_PLATFORM_CEILINGS if requested values > platform
```

## Shared-worker checklist

Before executing a trade for user U:

1. Load `UserPolicyContext` for U only  
2. `enforcer.assert_context_isolation(U, ctx)`  
3. Build auth with `user_id=U`, matching `wallet_id` / address  
4. `enforcer.authorize_trade(ctx, tx, auth, now, expected_chain_id=…)`  
5. Use `scoped_idempotency_key(U, raw_key)`  

Never reuse one portfolio object or auth object across users in the same process.
