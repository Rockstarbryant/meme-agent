from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from datetime import datetime, timezone

from runner import __version__
from runner.client import ControlPlaneClient, ControlPlaneError
from runner.runtime import RunnerRuntime
from runner.settings import RunnerSettings, _private_write, load_credentials, save_credentials
from runner.store import LocalStore
from runner.wallets import CircleCli, redact

from app.chains.arc.network import USDC_ERC20_ADDRESS


async def cmd_pair(s: RunnerSettings, a: argparse.Namespace) -> int:
    client = ControlPlaneClient(a.server or s.server_url)
    try:
        r = await client.pair(a.code, a.name or s.name, __version__)
    except ControlPlaneError as e:
        print(f"Pairing failed: {e.detail}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()
    path = save_credentials(s.state_dir, a.server or s.server_url, r.runner_id, r.token)
    print(f"Paired. Credentials saved to {path} (mode 600). The token only authenticates this runner to the control plane;\n"
          "it grants no wallet or signing authority. Start with: python -m runner run")
    return 0


async def cmd_run(s: RunnerSettings, a: argparse.Namespace) -> int:
    creds = load_credentials(s.state_dir)
    if not creds:
        print("Not paired. Run: python -m runner pair --server URL --code CODE", file=sys.stderr)
        return 1
    store = LocalStore(s.state_dir / "state.db")
    client = ControlPlaneClient(creds["server_url"], creds["token"])
    rt = RunnerRuntime(s, client, store)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, rt.stop)
    print(f"arc-runner {__version__} running against {creds['server_url']} | data={rt.data_kind} | wallet={rt.wallet.name if rt.wallet else 'none (PAPER only)'} | "
          f"LIVE locally {'ENABLED' if s.live_enabled else 'disabled'} | Ctrl+C to stop")
    await rt.run()
    await client.aclose()
    store.close()
    return 0


async def cmd_status(s: RunnerSettings, a: argparse.Namespace) -> int:
    creds = load_credentials(s.state_dir)
    store = LocalStore(s.state_dir / "state.db")
    rt = RunnerRuntime(s, ControlPlaneClient(creds["server_url"] if creds else s.server_url, creds["token"] if creds else None), store)
    live = await rt.live_status(force=True)
    print(json.dumps({"version": __version__, "paired": bool(creds), "server": creds["server_url"] if creds else None, "outbox_pending": store.outbox_size(),
                      "data_source": rt.data_kind, "wallet_provider": rt.wallet.name if rt.wallet else None, "live": live.model_dump(),
                      "local_ceilings": s.ceilings.model_dump(mode="json")}, indent=2, default=str))
    return 0


async def cmd_circle_verify(s: RunnerSettings, a: argparse.Namespace) -> int:
    """READ-ONLY capture of the real Circle CLI outputs from YOUR authenticated session, so schemas can be verified instead of guessed."""
    cli = CircleCli(s.circle_cli_path)
    plan = [("version", ["--version"]), ("wallet status", cli.status()), ("blockchain list", cli.blockchain_list()), ("wallet list", cli.wallet_list(s.circle_chain))]
    addr = s.circle_wallet_address
    if addr:
        plan += [("wallet balance", cli.balance(addr, s.circle_chain)), ("wallet limit", cli.limit(addr, s.circle_chain)), ("wallet limit budget", cli.budget(addr))]
        if a.include_estimate:  # --estimate does not broadcast
            plan.append(("execute --estimate (balanceOf)", cli.execute("balanceOf(address)", [addr], USDC_ERC20_ADDRESS, addr, s.circle_chain, estimate=True)))
    out = []
    for label, args in plan:
        r = await cli.run(args)
        out.append({"label": label, "argv": [cli.path, *args], "returncode": r.returncode, "stdout_json": redact(r.json) if r.json is not None else None,
                    "stdout_text": None if r.json is not None else r.stdout[:4000], "stderr_tail": r.stderr[-500:]})
        print(f"  {label}: exit {r.returncode}")
    path = s.state_dir / f"circle-capture-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json"
    _private_write(path, json.dumps({"cli": cli.path, "chain": s.circle_chain, "results": out}, indent=2))
    print(f"\nCaptured to {path} (secret-looking fields redacted). Review it, then share it to complete schema verification.\n"
          "Nothing was sent anywhere and nothing was broadcast. LIVE stays disabled until the provider is marked verified in code.")
    return 0


async def cmd_unpair(s: RunnerSettings, a: argparse.Namespace) -> int:
    p = s.state_dir / "credentials.json"
    if p.exists():
        p.unlink()
    print("Local credentials removed. Also revoke this runner in the web app.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="arc-runner", description="Local Runner for the Arc trading agent (executes on YOUR machine).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pair"); p.add_argument("--server"); p.add_argument("--code", required=True); p.add_argument("--name")
    sub.add_parser("run"); sub.add_parser("status"); sub.add_parser("unpair"); sub.add_parser("cloud-worker")
    v = sub.add_parser("circle-verify"); v.add_argument("--include-estimate", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.cmd == "cloud-worker":
        from runner.cloud_worker import run_cloud_worker
        return asyncio.run(run_cloud_worker())
    fn = {"pair": cmd_pair, "run": cmd_run, "status": cmd_status, "circle-verify": cmd_circle_verify, "unpair": cmd_unpair}[a.cmd]
    return asyncio.run(fn(RunnerSettings(), a))
