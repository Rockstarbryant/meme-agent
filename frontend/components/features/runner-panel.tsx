"use client";
import { useState } from "react";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { ErrorState, Loading } from "@/components/states";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useApi } from "@/hooks/use-api";
import { API_URL, api, toApiError, type ApiError } from "@/lib/api";
import { ago } from "@/lib/format";
import type { PairingCode, RunnerInfo } from "@/types/api";

const CEILING_LABELS: Record<string, string> = { max_trade_usdc: "max trade", max_position_usdc: "max position", max_daily_loss_usdc: "daily loss",
  max_total_exposure_usdc: "total exposure", max_open_positions: "open positions", max_slippage_pct: "slippage %", min_liquidity_usdc: "min liquidity" };

/** The Local Runner is where trading actually happens: on the user's machine, next to their own wallet session. */
export function RunnerPanel() {
  const res = useApi<RunnerInfo[]>("/runners", { intervalMs: 8000, refreshOn: ["RISK_ALERT", "AGENT_ERROR"] });
  const [code, setCode] = useState<PairingCode | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [revokeOpen, setRevokeOpen] = useState(false);

  if (res.loading && !res.data) return <Loading />;
  if (!res.data) return res.error ? <ErrorState error={res.error} onRetry={() => void res.reload()} /> : null;
  const runner = res.data.find((r) => !r.revoked_at) ?? null;

  async function createCode() {
    setBusy(true); setError(null);
    try { setCode(await api<PairingCode>("/runners/pairing-codes", { method: "POST" })); } catch (e) { setError(toApiError(e)); } finally { setBusy(false); }
  }
  async function revoke() {
    if (!runner) return;
    setBusy(true); setError(null);
    try { await api(`/runners/${runner.id}`, { method: "DELETE" }); setRevokeOpen(false); setCode(null); await res.reload(); } catch (e) { setError(toApiError(e)); } finally { setBusy(false); }
  }

  return (
    <Card>
      <CardHeader><CardTitle>Local Runner</CardTitle></CardHeader>
      <CardContent className="space-y-3 text-sm">
        <p className="text-muted-foreground">Trading runs on <strong>your own machine</strong>, next to your own wallet session. This site only sends policy and shows results: it never holds your funds, keys or wallet session. The runner connects out to this server; nothing connects into your machine.</p>
        {error && <ErrorState error={error} />}
        {runner === null ? (
          <div className="space-y-3">
            <Alert variant="warning">No runner is paired, so the agent cannot trade. PAPER mode needs a runner too, but no wallet.</Alert>
            {code ? (
              <div className="space-y-2 rounded-md border p-3">
                <p>Pairing code (single use, expires in {Math.round(code.expires_in / 60)} minutes):</p>
                <p className="font-mono text-lg font-semibold tracking-wider">{code.code}</p>
                <p className="text-xs text-muted-foreground">On the machine that will run the agent:</p>
                <pre className="overflow-x-auto rounded bg-muted p-2 text-xs">{`python -m runner pair --server ${API_URL} --code ${code.code}\npython -m runner run`}</pre>
                <p className="text-xs text-muted-foreground">Pairing gives the runner a token that only authenticates it to this server. It carries no wallet or signing authority.</p>
              </div>
            ) : <Button disabled={busy} onClick={() => void createCode()}>Add runner</Button>}
          </div>
        ) : (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{runner.name}</span>
              <Badge variant={runner.online ? "success" : "warning"}>{runner.online ? "ONLINE" : "OFFLINE"}</Badge>
              {runner.state && <Badge>{runner.state.replace("_", " ")}</Badge>}
              <span className="text-xs text-muted-foreground">v{runner.version || "?"} · last seen {ago(runner.last_seen_at)}</span>
            </div>
            <p className="text-xs text-muted-foreground">Config applied: v{runner.applied_config_version} of v{runner.desired_config_version}{runner.applied_config_version < runner.desired_config_version ? " (waiting for the runner to fetch it)" : ""}</p>
            {!runner.online && <Alert variant="warning">Your runner is not connected. Start it with <code>python -m runner run</code>. Open positions stay protected locally while it runs; without it nothing is monitored.</Alert>}
            {runner.entries_suspended_reason && <Alert variant="warning">New entries are suspended: {runner.entries_suspended_reason}</Alert>}
            {runner.last_error && <p className="text-xs text-destructive">Last error: {runner.last_error}</p>}
            <div>
              <p className="text-xs font-semibold">Wallet provider on the runner: {runner.wallet_provider ?? "none (PAPER only)"}</p>
              {runner.live && !runner.live.available && (
                <div className="mt-1"><p className="text-xs text-muted-foreground">LIVE is unavailable on this runner because:</p>
                  <ul className="list-disc pl-5 text-xs text-muted-foreground">{runner.live.reasons.map((r) => <li key={r}>{r}</li>)}</ul></div>
              )}
            </div>
            {Object.keys(runner.local_ceilings).length > 0 && (
              <div><p className="text-xs font-semibold">Local ceilings (enforced on your machine; the server cannot exceed them)</p>
                <dl className="mt-1 grid grid-cols-2 gap-x-4 gap-y-1 text-xs md:grid-cols-3">
                  {Object.entries(runner.local_ceilings).filter(([k]) => k in CEILING_LABELS).map(([k, v]) => <div key={k} className="flex justify-between gap-2"><dt className="text-muted-foreground">{CEILING_LABELS[k]}</dt><dd>{String(v)}</dd></div>)}
                </dl></div>
            )}
            <Button variant="destructive" size="sm" onClick={() => setRevokeOpen(true)}>Revoke runner</Button>
          </div>
        )}
        <ConfirmDialog open={revokeOpen} onOpenChange={setRevokeOpen} busy={busy} destructive title="Revoke this runner?" confirmLabel="Revoke runner"
          description="The runner is locked out immediately and the agent is set to STOPPED. Positions it holds are no longer managed by anything until you pair a runner again. If you retire the machine, also log out of the Circle CLI there." onConfirm={revoke} />
      </CardContent>
    </Card>
  );
}
