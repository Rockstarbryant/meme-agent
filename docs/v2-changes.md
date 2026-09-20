# V2 changes from the Claude baseline

## Modified
- `backend/app/domain/market.py` — added `pool_id` for Uniswap v4 market identity.
- `backend/app/chains/arc/adapter.py` — Arc RPC adapter now delegates quote/build/simulation to the Uniswap adapter while keeping live execution fail-closed.
- `backend/app/chains/arc/deployments.py` — replaced evidence-only deployment constants with current official Uniswap Arc deployments.
- `backend/app/launchpads/arc_candidates.py` — replaced the incorrect Pools/Arc assumption with the currently documented Arc launchpad contracts.
- `backend/app/chains/arc/market_data.py` — new real Arc Bitquery market/discovery implementation.
- `backend/app/chains/arc/uniswap.py` — new Uniswap Trading API + Arc Universal Router adapter and deterministic calldata decoder.
- `backend/app/integrations/bitquery.py` — new Bitquery V2 HTTP client.
- `backend/runner/settings.py` — Bitquery/Uniswap/Circle execution configuration.
- `backend/runner/runtime.py` — wires real Arc data, Uniswap quotes, and wallet policy into the Local Runner.
- `backend/runner/wallets.py` — real read-only Circle CLI integration and guarded `wallet execute` path.
- `backend/runner.env.example` — V2 environment configuration.
- `backend/requirements-runner.txt` — adds `eth-abi` for Universal Router calldata decoding.

## Added
- `docs/v2-audit.md`
- `docs/v2-changes.md`

## Intentionally not enabled
- Uniswap Permit2 typed-data signing through Circle CLI.
- Mainnet live trading flag.
- Blind fill parsing from Universal Router logs.

These remain explicit gates rather than being guessed into production code.
