"use client";
import { useState } from "react";
import Link from "next/link";
import { ActionBadge, ModeBadge } from "@/components/badges";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { ErrorState, Loading } from "@/components/states";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { RunnerPanel } from "@/components/features/runner-panel";
import { useApi } from "@/hooks/use-api";
import { api, toApiError, type ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { ago, usd } from "@/lib/format";
import type { AgentStatus, WalletState } from "@/types/api";

const LIVE_EVENTS = ["EMERGENCY_STOP_CHANGED", "POSITION_OPENED", "POSITION_CLOSED", "DECISION_RECORDED"];

export function EmergencyControl({ status, onDone }: { status: AgentStatus; onDone: () => void | Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const enable = !status.emergency_stop;
  async function run() {
    setBusy(true); setError(null);
    try { await api("/agent/emergency-stop", { method: "POST", body: { enabled: enable } }); setOpen(false); await onDone(); }
    catch (e) { setError(toApiError(e)); } finally { setBusy(false); }
  }
  return (
    <div className="space-y-2">
      <Button variant={enable ? "destructive" : "outline"} onClick={() => setOpen(true)}>{enable ? "EMERGENCY STOP" : "Disable emergency stop"}</Button>
      {error && <ErrorState error={error} />}
      <ConfirmDialog open={open} onOpenChange={setOpen} busy={busy} destructive={enable}
        title={enable ? "Enable emergency stop?" : "Disable emergency stop?"}
        description={enable ? "No new positions will be opened and no new BUY orders will be submitted. Existing positions keep their stop-loss, take-profit and trailing protection."
          : "The agent may open new positions again once you press START. Only do this when you are sure."}
        confirmLabel={enable ? "Enable emergency stop" : "Disable emergency stop"} onConfirm={run} />
    </div>
  );
}

export function AgentControl() {
  const { refresh } = useAuth();
  const status = useApi<AgentStatus>("/agent", { refreshOn: LIVE_EVENTS, intervalMs: 10000 });
  const wallet = useApi<WalletState>("/wallet");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [liveOpen, setLiveOpen] = useState(false);

  if (status.loading && !status.data) return <Loading />;
  if (!status.data) return status.error ? <ErrorState error={status.error} onRetry={() => void status.reload()} /> : null;
  const s = status.data;

  async function act(path: string, body?: unknown) {
    setBusy(path); setError(null);
    try { await api(path, { method: "POST", body }); await status.reload(); await refresh(); return true; }
    catch (e) { setError(toApiError(e)); return false; } finally { setBusy(null); }
  }
  async function switchMode(target: "LIVE" | "PAPER") {
    const ok = await act("/agent/mode", { mode: target, confirmation: target === "LIVE" ? s.live_confirmation_phrase : "" });
    if (ok) setLiveOpen(false);
  }
  const running = s.desired_state === "RUNNING";
  const pol = wallet.data?.policy;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader><CardTitle>Agent</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-center gap-2"><ModeBadge mode={s.mode} /><Badge variant={s.state === "RUNNING" ? "success" : s.state === "OFFLINE" || s.state === "LIVE_BLOCKED" ? "warning" : "default"}>{s.state.replace("_", " ")}</Badge>
            {s.emergency_stop && <Badge variant="solidDestructive">EMERGENCY STOP</Badge>}</div>
          <div className="flex flex-wrap gap-2">
            <Button disabled={running || s.emergency_stop || busy !== null} onClick={() => void act("/agent/start")}>Start</Button>
            <Button variant="outline" disabled={!running || busy !== null} onClick={() => void act("/agent/pause")}>Pause</Button>
            <Button variant="outline" disabled={s.desired_state === "STOPPED" || busy !== null} onClick={() => void act("/agent/stop")}>Stop</Button>
            <EmergencyControl status={s} onDone={() => status.reload()} />
          </div>
          <p className="text-xs text-muted-foreground">Requested: <strong>{s.desired_state}</strong> · Reported by your runner: <strong>{s.state.replace("_", " ")}</strong>{s.applied_config_version < s.config_version ? " · waiting for the runner to apply your latest change" : ""}. Commands are delivered to your Local Runner within seconds.</p>
          {s.state === "OFFLINE" && <Alert variant="warning">Your Local Runner is offline, so nothing is trading or being monitored. Start it with <code>python -m runner run</code>.</Alert>}
          {s.state === "LIVE_BLOCKED" && <Alert variant="warning">LIVE was requested but your runner refused to trade LIVE (it never falls back to PAPER). See the reasons below.</Alert>}
          <p className="text-xs text-muted-foreground">Pause and Stop halt new entries only. Open positions stay protected by stop-loss, take-profit and trailing logic on your runner.</p>
          {error && !liveOpen && <ErrorState error={error} />}
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm md:grid-cols-3">
            <div><dt className="text-xs text-muted-foreground">Strategy</dt><dd>{s.strategy.id} v{s.strategy.version}</dd></div>
            <div><dt className="text-xs text-muted-foreground">Data source</dt><dd>{s.data_source}</dd></div>
            <div><dt className="text-xs text-muted-foreground">Data status</dt><dd>{s.data_status}</dd></div>
            <div><dt className="text-xs text-muted-foreground">AI</dt><dd>{s.ai.mode}{s.ai.provider ? ` (${s.ai.provider} / ${s.ai.model})` : ""}</dd></div>
            <div><dt className="text-xs text-muted-foreground">Last activity</dt><dd>{ago(s.last_activity_at)}</dd></div>
            <div><dt className="text-xs text-muted-foreground">Open positions</dt><dd>{s.open_positions}</dd></div>
          </dl>
          {s.last_decision && (
            <div className="rounded-md border p-2 text-sm"><p className="mb-1 text-xs text-muted-foreground">Last decision</p>
              <div className="flex flex-wrap items-center gap-2"><ActionBadge action={s.last_decision.action} /><span>{s.last_decision.symbol ?? s.last_decision.token}</span>
                <Link className="text-xs underline" href={`/decisions/${s.last_decision.id}`}>why?</Link></div>
              <p className="mt-1 text-xs text-muted-foreground">{s.last_decision.reason}</p></div>
          )}
        </CardContent>
      </Card>

      <RunnerPanel />

      <Card>
        <CardHeader><CardTitle>Wallet and policy</CardTitle></CardHeader>
        <CardContent className="space-y-2 text-sm">
          {wallet.data ? (<>
            <p>Execution: <strong>{wallet.data.execution_capability.label}</strong></p>
            <p>Authorization: {wallet.data.authorization ? `${wallet.data.authorization.capability} (active)` : "none"}</p>
            <p>Policy: {pol ? `max trade ${usd(pol.max_trade_usdc)}, max position ${usd(pol.max_position_usdc)}, daily loss ${usd(pol.max_daily_loss_usdc)}` : "not configured"}</p>
          </>) : <Loading />}
          <p className="text-xs text-muted-foreground">Effective risk limits (stricter of app limits and wallet policy): max trade {usd(s.limits.max_trade_usdc)}, max position {usd(s.limits.max_position_usdc)}, daily loss {usd(s.limits.max_daily_loss_usdc)}, max open {s.limits.max_open_positions}, slippage {s.limits.max_slippage_pct}%.</p>
          <Link className="text-xs underline" href="/wallet">Manage wallet</Link>
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>LIVE trading</CardTitle></CardHeader>
        <CardContent className="space-y-3 text-sm">
          {s.mode === "LIVE" ? (<>
            <Alert variant="destructive">LIVE MODE is active. Trades use real funds within your wallet policy.</Alert>
            <Button variant="outline" onClick={() => void switchMode("PAPER")}>Switch back to PAPER</Button>
          </>) : (<>
            <p>LIVE is off by default and needs explicit confirmation. It stays refused until every requirement below is met.</p>
            {s.live_blockers.length > 0 && (<div><p className="font-medium">Currently blocked because:</p><ul className="list-disc pl-5 text-muted-foreground">{s.live_blockers.map((b) => <li key={b}>{b}</li>)}</ul></div>)}
            <Button variant="destructive" onClick={() => { setError(null); setLiveOpen(true); }}>Switch to LIVE…</Button>
          </>)}
        </CardContent>
      </Card>
      <ConfirmDialog open={liveOpen} onOpenChange={setLiveOpen} destructive busy={busy !== null} phrase={s.live_confirmation_phrase}
        title="Enable LIVE trading?" description="This is real money on Arc. The server re-checks every requirement and will refuse if any is unmet. Nothing is submitted until the wallet, policy and integration checks pass."
        confirmLabel="Enable LIVE" extra={error ? <ErrorState error={error} /> : null} onConfirm={() => switchMode("LIVE")} />
    </div>
  );
}
