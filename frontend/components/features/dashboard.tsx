"use client";
import { useMemo, useState } from "react";
import Link from "next/link";
import { Activity, ArrowDownRight, ArrowUpRight, Layers, ShieldCheck, Target, Wallet as WalletIcon } from "lucide-react";
import { ActionBadge, ModeBadge, SimulatedLabel } from "@/components/badges";
import { PageHeader } from "@/components/page-header";
import { AreaChart, Sparkline } from "@/components/charts/area-chart";
import { exitReasonLabel } from "@/components/notification-watcher";
import { ErrorState, Loading } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useApi } from "@/hooks/use-api";
import { changePct, type Range } from "@/lib/chart";
import { ago, duration, pct, plainPct, pnlClass, signedUsd, usd } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { AgentStatus, Opportunity, Order, PerformanceStats, Portfolio, PortfolioHistory, Position } from "@/types/api";

const LIVE = ["POSITION_OPENED", "POSITION_CLOSED", "ORDER_FILLED", "DECISION_RECORDED", "EMERGENCY_STOP_CHANGED"];
const RANGES: { id: Range; label: string }[] = [{ id: "1h", label: "1H" }, { id: "6h", label: "6H" }, { id: "24h", label: "24H" }, { id: "7d", label: "7D" }, { id: "all", label: "All" }];

function Tile({ icon: Icon, label, value, sub, spark, up, valueClass }: { icon: typeof Activity; label: string; value: React.ReactNode; sub?: React.ReactNode; spark?: number[]; up?: boolean; valueClass?: string }) {
  return (
    <Card className="overflow-hidden"><CardContent className="space-y-1 p-4">
      <div className="flex items-center justify-between gap-2"><span className="flex items-center gap-1.5 text-xs text-muted-foreground"><Icon className="h-3.5 w-3.5" aria-hidden />{label}</span>
        {spark && <Sparkline values={spark} up={up ?? true} />}</div>
      <p className={cn("text-xl font-semibold tabular-nums", valueClass)}>{value}</p>
      {sub && <p className="text-xs text-muted-foreground">{sub}</p>}
    </CardContent></Card>
  );
}

/** Thin stacked bar: how the money is split. */
function SplitBar({ parts }: { parts: { label: string; value: number; className: string }[] }) {
  const total = parts.reduce((a, p) => a + Math.max(0, p.value), 0) || 1;
  return (
    <div className="space-y-2">
      <div className="flex h-3 overflow-hidden rounded-full bg-muted" role="img" aria-label={parts.map((p) => `${p.label} ${usd(p.value)}`).join(", ")}>
        {parts.map((p) => <div key={p.label} className={p.className} style={{ width: `${(Math.max(0, p.value) / total) * 100}%` }} />)}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">{parts.map((p) => (
        <span key={p.label} className="flex items-center gap-1.5"><span className={cn("h-2 w-2 rounded-full", p.className)} />{p.label} <strong className="tabular-nums">{usd(p.value)}</strong></span>))}</div>
    </div>
  );
}

function Meter({ label, value, max, right, danger }: { label: string; value: number; max: number; right: string; danger?: boolean }) {
  const f = max > 0 ? Math.min(100, Math.max(0, (value / max) * 100)) : 0;
  const tone = danger ? (f > 80 ? "bg-destructive" : f > 50 ? "bg-warning" : "bg-success") : "bg-primary";
  return (
    <div className="space-y-1"><div className="flex justify-between text-xs"><span className="text-muted-foreground">{label}</span><span className="tabular-nums">{right}</span></div>
      <div className="h-2 overflow-hidden rounded-full bg-muted" role="progressbar" aria-label={label} aria-valuenow={Math.round(f)} aria-valuemin={0} aria-valuemax={100}><div className={cn("h-2 rounded-full transition-all", tone)} style={{ width: `${f}%` }} /></div></div>
  );
}

export function Dashboard() {
  const [range, setRange] = useState<Range>("24h");
  const pf = useApi<Portfolio>("/portfolio", { refreshOn: LIVE, intervalMs: 10000 });
  const hist = useApi<PortfolioHistory>(`/portfolio/history?range=${range}`, { refreshOn: LIVE, intervalMs: 30000 });
  const perf = useApi<PerformanceStats>("/portfolio/performance", { refreshOn: ["POSITION_CLOSED"], intervalMs: 60000 });
  const agent = useApi<AgentStatus>("/agent", { refreshOn: LIVE, intervalMs: 10000 });
  const open = useApi<Position[]>("/positions?status=OPEN", { refreshOn: ["POSITION_OPENED", "POSITION_UPDATED", "POSITION_CLOSED"], intervalMs: 15000 });
  const opps = useApi<Opportunity[]>("/opportunities?limit=100", { refreshOn: ["DECISION_RECORDED"] });
  const orders = useApi<Order[]>("/orders?limit=5", { refreshOn: ["ORDER_FILLED"] });

  const series = useMemo(() => (hist.data?.points ?? []).map((q) => ({ t: q.t, v: q.v })), [hist.data]);
  const spark = useMemo(() => series.filter((_, i) => i % Math.max(1, Math.floor(series.length / 24)) === 0).map((q) => q.v), [series]);
  const counts = useMemo(() => {
    const acc = { BUY: 0, WATCH: 0, REJECT: 0 } as Record<string, number>;
    for (const o of opps.data ?? []) acc[o.final_action] = (acc[o.final_action] ?? 0) + 1;
    return acc;
  }, [opps.data]);

  if (pf.loading && !pf.data) return <Loading />;
  if (!pf.data) return pf.error ? <ErrorState error={pf.error} onRetry={() => void pf.reload()} /> : null;
  const p = pf.data, a = agent.data, lim = p.limits, st = perf.data;
  const total = p.realized_pnl_usdc + p.unrealized_pnl_usdc;
  const sinceStart = changePct(p.starting_cash_usdc, p.total_value_usdc);
  const rangeChange = changePct(hist.data?.first, hist.data?.last);
  const up = (rangeChange ?? sinceStart ?? 0) >= 0;
  const recent = (opps.data ?? []).slice(0, 5);

  return (
    <div className="space-y-4">
      <PageHeader title="Dashboard" description={`Your ${p.label === "PAPER" ? "paper" : "live"} portfolio at a glance.`} />
      {/* Hero: the one number that matters, with its chart */}
      <Card className="overflow-hidden border-primary/30 bg-gradient-to-br from-primary/10 via-background to-background">
        <CardContent className="space-y-3 p-4 md:p-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <span>Portfolio value</span>{a && <ModeBadge mode={a.mode} />}
                {a && <Badge variant={a.state === "RUNNING" ? "success" : a.state === "OFFLINE" ? "warning" : "default"}>{a.state.replace("_", " ")}</Badge>}
              </div>
              <p className="text-4xl font-bold tabular-nums tracking-tight md:text-5xl">{usd(p.total_value_usdc)}</p>
              <p className={cn("mt-1 flex items-center gap-1 text-sm font-medium tabular-nums", pnlClass(total))}>
                {total >= 0 ? <ArrowUpRight className="h-4 w-4" aria-hidden /> : <ArrowDownRight className="h-4 w-4" aria-hidden />}
                {signedUsd(total)} {sinceStart != null && <span>({pct(sinceStart, 2)})</span>}<span className="font-normal text-muted-foreground">since start</span>
              </p>
              <p className="text-xs text-muted-foreground">Started with {usd(p.starting_cash_usdc)}{p.starting_cash_derived ? " (reconstructed from current value and P&L)" : ""}</p>
            </div>
            <div className="flex gap-1" role="group" aria-label="Chart range">{RANGES.map((r) => (
              <Button key={r.id} size="sm" variant={range === r.id ? "default" : "ghost"} aria-pressed={range === r.id} onClick={() => setRange(r.id)}>{r.label}</Button>))}</div>
          </div>
          <AreaChart points={series} range={range} baseline={p.starting_cash_usdc > 0 ? p.starting_cash_usdc : null} label="Portfolio value" height={230} />
          {hist.data && hist.data.high != null && (
            <div className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground">
              <span>Range {rangeChange != null ? <strong className={pnlClass(rangeChange)}>{pct(rangeChange, 2)}</strong> : "—"}</span>
              <span>High <strong className="text-foreground tabular-nums">{usd(hist.data.high)}</strong></span>
              <span>Low <strong className="text-foreground tabular-nums">{usd(hist.data.low)}</strong></span>
              <span>{hist.data.samples.toLocaleString()} samples</span>
            </div>)}
        </CardContent>
      </Card>

      {/* KPI tiles */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Tile icon={WalletIcon} label="Available" value={usd(p.cash_usdc)} sub={p.label === "PAPER" ? "virtual USDC" : "wallet USDC"} />
        <Tile icon={Layers} label="Open positions" value={`${p.open_positions} / ${lim.max_open_positions}`} sub={<Link className="underline" href="/positions">view positions</Link>} />
        <Tile icon={Activity} label="Today" value={signedUsd(p.daily_pnl_usdc)} valueClass={pnlClass(p.daily_pnl_usdc)} sub={`realized ${signedUsd(p.realized_pnl_usdc)} · open ${signedUsd(p.unrealized_pnl_usdc)}`} spark={spark} up={up} />
        <Tile icon={Target} label="Win rate" value={st?.win_rate_pct != null ? plainPct(st.win_rate_pct, 0) : "—"} sub={st && st.closed ? `${st.wins}W / ${st.losses}L of ${st.closed} closed` : "no closed trades yet"} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* Where the money is */}
        <Card><CardHeader><CardTitle>Allocation and risk</CardTitle></CardHeader><CardContent className="space-y-4">
          <SplitBar parts={[{ label: "In positions", value: p.exposure_usdc, className: "bg-primary" }, { label: "Cash", value: p.cash_usdc, className: "bg-muted-foreground/40" }]} />
          <Meter label="Daily loss used" value={Math.max(0, -p.daily_pnl_usdc)} max={lim.max_daily_loss_usdc} right={`${usd(Math.max(0, -p.daily_pnl_usdc))} of ${usd(lim.max_daily_loss_usdc)}`} danger />
          <Meter label="Exposure" value={p.exposure_usdc} max={lim.max_total_exposure_usdc} right={`${usd(p.exposure_usdc)} of ${usd(lim.max_total_exposure_usdc)}`} />
          <p className="flex items-center gap-1.5 text-xs text-muted-foreground"><ShieldCheck className="h-3.5 w-3.5" aria-hidden />Max trade {usd(lim.max_trade_usdc)} · max position {usd(lim.max_position_usdc)} · slippage {lim.max_slippage_pct}%</p>
        </CardContent></Card>

        {/* Performance */}
        <Card><CardHeader><CardTitle>Performance</CardTitle></CardHeader><CardContent className="space-y-3 text-sm">
          {!st || st.closed === 0 ? <p className="text-muted-foreground">Stats appear after the first position closes.</p> : (<>
            <div className="grid grid-cols-3 gap-3">
              <div><p className="text-xs text-muted-foreground">Profit factor</p><p className="font-semibold tabular-nums">{st.profit_factor != null ? st.profit_factor.toFixed(2) : "—"}</p></div>
              <div><p className="text-xs text-muted-foreground">Avg win</p><p className={cn("font-semibold tabular-nums", pnlClass(st.avg_win_usdc))}>{signedUsd(st.avg_win_usdc)}</p></div>
              <div><p className="text-xs text-muted-foreground">Avg loss</p><p className={cn("font-semibold tabular-nums", pnlClass(st.avg_loss_usdc))}>{signedUsd(st.avg_loss_usdc)}</p></div>
              <div><p className="text-xs text-muted-foreground">Best</p><p className="font-semibold tabular-nums text-success">{signedUsd(st.best_usdc)}</p></div>
              <div><p className="text-xs text-muted-foreground">Worst</p><p className="font-semibold tabular-nums text-destructive">{signedUsd(st.worst_usdc)}</p></div>
              <div><p className="text-xs text-muted-foreground">Avg hold</p><p className="font-semibold tabular-nums">{duration(st.avg_hold_seconds)}</p></div>
            </div>
            <div className="space-y-1.5"><p className="text-xs text-muted-foreground">How trades ended</p>
              {st.by_exit_reason.slice(0, 5).map((g) => (
                <div key={g.key} className="flex items-center justify-between gap-2 text-xs"><span>{exitReasonLabel(g.key)}</span>
                  <span className="tabular-nums text-muted-foreground">{g.count}× · <span className={pnlClass(g.pnl_usdc)}>{signedUsd(g.pnl_usdc)}</span></span></div>))}</div>
            {st.by_strategy.length > 0 && <p className="text-xs text-muted-foreground">By strategy: {st.by_strategy.map((g) => `${g.key.replaceAll("_", " ")} ${g.count} (${plainPct(g.win_rate_pct, 0)} win)`).join(" · ")}</p>}
          </>)}
        </CardContent></Card>
      </div>

      {/* Open positions */}
      <Card><CardHeader><div className="flex items-center justify-between"><CardTitle>Open positions</CardTitle><Link className="text-xs underline" href="/positions">all positions</Link></div></CardHeader><CardContent className="space-y-2">
        {(open.data ?? []).length === 0 && <p className="text-sm text-muted-foreground">Nothing open. {a?.state === "RUNNING" ? "The agent is scanning for entries." : "Start the agent to begin."}</p>}
        {(open.data ?? []).slice(0, 5).map((x) => {
          const pnlv = x.unrealized_pnl_usdc;
          return (
            <Link key={x.id} href="/positions" className="flex items-center gap-3 rounded-md border p-2 text-sm hover:bg-muted/50">
              <div className="min-w-0 flex-1"><p className="truncate font-medium">{x.symbol ?? x.token_address.slice(0, 8)}</p><p className="text-xs text-muted-foreground">{ago(x.opened_at)} · {x.strategy_id.replaceAll("_", " ")}</p></div>
              <div className="text-right tabular-nums"><p className={cn("font-semibold", pnlClass(pnlv))}>{signedUsd(pnlv)}</p><p className={cn("text-xs", pnlClass(x.gain_pct))}>{pct(x.gain_pct)}</p></div>
            </Link>);
        })}
      </CardContent></Card>

      {/* Activity */}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card><CardHeader><CardTitle>Latest decisions</CardTitle></CardHeader><CardContent className="space-y-2">
          <div className="flex gap-2 text-xs"><Badge variant="success">{counts.BUY ?? 0} buy</Badge><Badge variant="warning">{counts.WATCH ?? 0} watch</Badge><Badge>{counts.REJECT ?? 0} reject</Badge><span className="text-muted-foreground">last 24h</span></div>
          {recent.length === 0 && <p className="text-sm text-muted-foreground">No decisions yet. Start the agent to begin evaluating tokens.</p>}
          {recent.map((o) => (
            <div key={o.decision_id} className="flex flex-wrap items-center gap-2 text-sm"><ActionBadge action={o.final_action} /><span className="font-medium">{o.token_name ?? o.symbol ?? o.token_key}</span>
              <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">{o.final_reason.slice(0, 60)}</span><Link className="text-xs underline" href={`/decisions/${o.decision_id}`}>why?</Link></div>))}
        </CardContent></Card>

        <Card><CardHeader><CardTitle>Recent trades</CardTitle></CardHeader><CardContent className="space-y-2">
          {(orders.data ?? []).length === 0 && <p className="text-sm text-muted-foreground">No trades yet.</p>}
          {(orders.data ?? []).map((o) => (
            <div key={o.id} className="flex flex-wrap items-center gap-2 text-sm"><SimulatedLabel simulated={o.simulated} />
              <span className={cn("font-medium", o.side === "BUY" ? "text-primary" : "text-success")}>{o.side}</span>
              <span className="text-muted-foreground">{usd((o.filled_quantity ?? 0) * (o.avg_price ?? 0))}</span><span className="text-xs text-muted-foreground">{o.status.toLowerCase()}</span>
              <span className="ml-auto text-xs text-muted-foreground">{ago(o.at)}</span></div>))}
        </CardContent></Card>
      </div>
    </div>
  );
}
