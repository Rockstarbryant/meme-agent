"use client";
import Link from "next/link";
import { ActionBadge, ModeBadge, SimulatedLabel } from "@/components/badges";
import { ErrorState, Loading, Stat } from "@/components/states";
import { PriceChart } from "@/components/price-chart";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useApi } from "@/hooks/use-api";
import { ago, pnlClass, signedUsd, usd } from "@/lib/format";
import type { AgentStatus, ChainInfo, Opportunity, Order, Portfolio, WalletState } from "@/types/api";

const LIVE = ["POSITION_OPENED", "POSITION_CLOSED", "ORDER_FILLED", "DECISION_RECORDED", "EMERGENCY_STOP_CHANGED"];

export function Dashboard() {
  const pf = useApi<Portfolio>("/portfolio", { refreshOn: LIVE, intervalMs: 10000 });
  const agent = useApi<AgentStatus>("/agent", { refreshOn: LIVE, intervalMs: 10000 });
  const opps = useApi<Opportunity[]>("/opportunities?limit=100", { refreshOn: ["DECISION_RECORDED"] });
  const orders = useApi<Order[]>("/orders?limit=5", { refreshOn: ["ORDER_FILLED"] });
  const chains = useApi<ChainInfo[]>("/chains");
  const wallet = useApi<WalletState>("/wallet");

  if (pf.loading && !pf.data) return <Loading />;
  if (!pf.data) return pf.error ? <ErrorState error={pf.error} onRetry={() => void pf.reload()} /> : null;
  const p = pf.data, a = agent.data, arc = chains.data?.find((c) => c.id === "arc");
  const lim = p.limits;
  const lossUsed = lim.max_daily_loss_usdc ? Math.min(100, Math.max(0, -p.daily_pnl_usdc) / lim.max_daily_loss_usdc * 100) : 0;
  const recent = (opps.data ?? []).slice(0, 5);

  return (
    <div className="space-y-4">
      <Card><CardContent className="grid grid-cols-2 gap-4 pt-4 md:grid-cols-4">
        <Stat label={`Portfolio value (${p.label})`} value={usd(p.total_value_usdc)} sub={`started ${usd(p.starting_cash_usdc)}`} />
        <Stat label="Available USDC" value={usd(p.cash_usdc)} sub={p.label === "PAPER" ? "virtual" : "wallet"} />
        <Stat label="P&L (realized / unrealized)" value={<span className={pnlClass(p.realized_pnl_usdc + p.unrealized_pnl_usdc)}>{signedUsd(p.realized_pnl_usdc + p.unrealized_pnl_usdc)}</span>} sub={`${signedUsd(p.realized_pnl_usdc)} / ${signedUsd(p.unrealized_pnl_usdc)}`} />
        <Stat label="Open positions" value={`${p.open_positions} / ${lim.max_open_positions}`} sub={<Link className="underline" href="/positions">view</Link>} />
      </CardContent></Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card><CardHeader><CardTitle>Portfolio value</CardTitle></CardHeader><CardContent>
          <PriceChart points={p.snapshots.map((s) => ({ at: s.at, price: s.total_value_usdc }))} /></CardContent></Card>
        <Card><CardHeader><CardTitle>Risk limits</CardTitle></CardHeader><CardContent className="space-y-1 text-sm">
          <p>Max trade {usd(lim.max_trade_usdc)} · Max position {usd(lim.max_position_usdc)} · Slippage {lim.max_slippage_pct}%</p>
          <p>Daily loss {usd(Math.max(0, -p.daily_pnl_usdc))} of {usd(lim.max_daily_loss_usdc)} used</p>
          <div className="h-2 rounded bg-muted" role="progressbar" aria-valuenow={Math.round(lossUsed)} aria-valuemin={0} aria-valuemax={100}><div className="h-2 rounded bg-destructive" style={{ width: `${lossUsed}%` }} /></div>
          <p className="text-xs text-muted-foreground">Daily P&L {signedUsd(p.daily_pnl_usdc)} · Max exposure {usd(lim.max_total_exposure_usdc)}</p></CardContent></Card>
        <Card><CardHeader><CardTitle>Agent</CardTitle></CardHeader><CardContent className="space-y-1 text-sm">
          {a ? (<><div className="flex flex-wrap gap-2"><ModeBadge mode={a.mode} /><Badge variant={a.state === "RUNNING" ? "success" : "default"}>{a.state.replace("_", " ")}</Badge><Badge variant={a.runner?.online ? "success" : "warning"}>{a.runner === null ? "NO RUNNER" : a.runner.online ? "RUNNER ONLINE" : "RUNNER OFFLINE"}</Badge></div>
            <p>Strategy: {a.strategy.id} v{a.strategy.version}</p><p>Data: {a.data_source} ({a.data_status})</p><p>Last activity: {ago(a.last_activity_at)}</p>
            <Link className="text-xs underline" href="/agent">Agent control</Link></>) : <Loading />}</CardContent></Card>
        <Card><CardHeader><CardTitle>Wallet and network</CardTitle></CardHeader><CardContent className="space-y-1 text-sm">
          <p>Wallet: <strong>{wallet.data?.execution_capability.label ?? "…"}</strong></p>
          <p>Arc network: {arc?.network_status ? (arc.network_status.ok ? <Badge variant="success">online (block {arc.network_status.block})</Badge> : <Badge variant="destructive">unavailable</Badge>) : "…"}</p>
          {arc && !arc.live_trading_verified && <p className="text-xs text-muted-foreground">LIVE trading integration not verified: PAPER only.</p>}
          <Link className="text-xs underline" href="/wallet">Wallet</Link></CardContent></Card>
      </div>

      <Card><CardHeader><CardTitle>Recent decisions ({(opps.data ?? []).length} opportunities)</CardTitle></CardHeader><CardContent className="space-y-2">
        {recent.length === 0 && <p className="text-sm text-muted-foreground">No decisions yet. Start the agent to begin evaluating tokens.</p>}
        {recent.map((o) => (<div key={o.decision_id} className="flex flex-wrap items-center gap-2 text-sm"><ActionBadge action={o.final_action} /><span className="font-medium">{o.symbol ?? o.token_key}</span>
          <span className="text-xs text-muted-foreground">{o.final_reason.slice(0, 70)}</span><Link className="ml-auto text-xs underline" href={`/decisions/${o.decision_id}`}>why?</Link></div>))}</CardContent></Card>

      <Card><CardHeader><CardTitle>Recent trades</CardTitle></CardHeader><CardContent className="space-y-2">
        {(orders.data ?? []).length === 0 && <p className="text-sm text-muted-foreground">No trades yet.</p>}
        {(orders.data ?? []).map((o) => (<div key={o.id} className="flex flex-wrap items-center gap-2 text-sm"><SimulatedLabel simulated={o.simulated} /><span>{o.side}</span><span>{o.status}</span>
          <span className="text-muted-foreground">{usd((o.filled_quantity ?? 0) * (o.avg_price ?? 0))}</span><span className="ml-auto text-xs text-muted-foreground">{ago(o.at)}</span></div>))}</CardContent></Card>
    </div>
  );
}
