# Wallet architecture (non-custodial)
`WalletProvider` abstracts the wallet; the execution engine never touches keys. Every submission is re-validated by
`PolicyValidator` in the base class (authorization active/not revoked, chain + chain id, router allowlist (empty = deny all),
max trade, slippage, tx built by a deterministic adapter).

| Capability | Provider | State |
|---|---|---|
| Paper only | `PaperWalletProvider` | works; cannot submit transactions |
| Explicit per-trade signing | `BrowserWalletProvider` | works; backend queues an unsigned tx, the user's wallet signs, backend never signs |
| Autonomous delegated | `AgentWalletProvider` | **disabled**: Circle/Arc agent-wallet/delegation API not verified (docs/arc-integration.md) |

Flow: connect wallet (EIP-191 challenge/response proves ownership) -> configure policy -> authorize (per-trade signing) ->
agent operates only inside the policy -> revoke any time (`POST /wallet/revoke`; also revoke token approvals in your wallet).
Any policy change voids existing authorizations. The backend never silently falls back to custodial keys.
**Funding the agent wallet** is not implemented: it depends on the unverified agent-wallet mechanism. PAPER uses virtual USDC.
**Live execution** (when unlocked) = fail-closed preflight -> quote -> allowlisted router -> simulate -> wallet policy check ->
submit -> wait for receipt -> parse the actual fill -> update portfolio. TIMEOUT never resubmits blindly.
