"use client";
import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { exitReasonLabel } from "@/components/notification-watcher";
import { useToast } from "@/components/toast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ModeTabs, type HistoryMode } from "@/components/mode-tabs";
import { Empty, ErrorState, Loading } from "@/components/states";
import { useApi } from "@/hooks/use-api";
import { api, toApiError, type ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { waitForCommand } from "@/lib/commands";
import { duration, pct, pnlClass, price, shortAddr, signedUsd, usd } from "@/lib/format";
import type { Position } from "@/types/api";

const stamp = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—");
const TP_TIERS_LABEL = (hit: number[]) => (hit.length ? `tier${hit.length > 1 ? "s" : ""} ${hit.map((t) => t + 1).join(", ")} hit` : "none hit");

function Field({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  return <div className={className}><dt className="text-xs text-muted-foreground">{label}</dt><dd className="break-words">{children}</dd></div>;
}

export function PositionCard({ p, onClose }: { p: Position; onClose?: (p: Position) => void }) {
  const total = p.realized_pnl_usdc + p.unrealized_pnl_usdc;
  const open = p.status === "OPEN";
  const name = p.symbol ?? shortAddr(p.token_address);
  const key = `${p.chain}:${p.token_address}`;
  // Held time for an open position ticks on its own clock (kept out of render so the card stays pure).
  const [nowMs, setNowMs] = useState(0);
  useEffect(() => { const tick = () => setNowMs(Date.now()); tick(); const t = setInterval(tick, 30000); return () => clearInterval(t); }, []);
  const held = p.status === "CLOSED" ? (p.held_seconds ?? null) : (nowMs && p.opened_at ? Math.max(0, (nowMs - new Date(p.opened_at).getTime()) / 1000) : (p.held_seconds ?? null));
  const ret = open ? (p.cost_basis_usdc ? (p.unrealized_pnl_usdc / p.cost_basis_usdc) * 100 : null) : (p.return_pct ?? null);
  return (
    <Card><CardContent className="space-y-3 pt-4">
      <div className="flex flex-wrap items-center gap-2">
        <Link className="font-semibold underline-offset-2 hover:underline" href={`/tokens/${encodeURIComponent(key)}`}>{name}</Link>
        <Badge variant={p.mode === "LIVE" ? "solidDestructive" : "solidPrimary"}>{p.mode}</Badge>
        <Badge variant={open ? "success" : "default"}>{p.status}</Badge>
        <span className={`ml-auto text-sm font-semibold ${pnlClass(total)}`}>{signedUsd(total)}{ret != null ? ` (${pct(ret)})` : ""}</span>
      </div>

      {!open && (
        <div className="rounded-md border bg-muted/40 p-2 text-sm">
          <p><span className="text-muted-foreground">Closed because: </span><strong>{exitReasonLabel(p.exit_reason)}</strong></p>
          <p className="text-xs text-muted-foreground">Closed {stamp(p.closed_at)} · held {duration(held)}</p>
        </div>
      )}

      <dl className="grid grid-cols-2 gap-x-3 gap-y-2 text-sm md:grid-cols-4">
        <Field label="Opened">{stamp(p.opened_at)}</Field>
        <Field label={open ? "Held for" : "Closed"}>{open ? duration(held) : stamp(p.closed_at)}</Field>
        <Field label="Entry price">{price(p.entry_price)}</Field>
        <Field label={open ? "Current price" : "Exit price"}>
          {open ? <>{price(p.last_price)} <span className={pnlClass(p.gain_pct)}>{pct(p.gain_pct)}</span></> : price(p.exit_price ?? p.last_price)}
        </Field>
        <Field label="Invested">{usd(p.invested_usdc ?? p.cost_basis_usdc)}</Field>
        <Field label={open ? "Value now" : "Proceeds"}>{open ? usd(p.cost_basis_usdc + p.unrealized_pnl_usdc) : usd(p.proceeds_usdc)}</Field>
        <Field label="Realized P&L"><span className={pnlClass(p.realized_pnl_usdc)}>{signedUsd(p.realized_pnl_usdc)}</span></Field>
        <Field label="Unrealized P&L"><span className={pnlClass(p.unrealized_pnl_usdc)}>{open ? signedUsd(p.unrealized_pnl_usdc) : "—"}</span></Field>
        <Field label="Peak price">{price(p.peak_price)}{open && p.drawdown_from_peak_pct != null ? <span className="text-xs text-muted-foreground"> ({pct(p.drawdown_from_peak_pct)} from peak)</span> : null}</Field>
        <Field label="Take-profit">{TP_TIERS_LABEL(p.tiers_hit)}</Field>
        <Field label="Quantity left">{p.quantity.toLocaleString(undefined, { maximumSignificantDigits: 6 })}{p.initial_quantity !== p.quantity ? <span className="text-xs text-muted-foreground"> of {p.initial_quantity.toLocaleString(undefined, { maximumSignificantDigits: 6 })}</span> : null}</Field>
        <Field label="Strategy">{p.strategy_id.replaceAll("_", " ")} v{p.strategy_version}</Field>
      </dl>

      <div className="flex flex-wrap items-center gap-3">
        {open && onClose && <Button size="sm" variant="outline" onClick={() => onClose(p)}>Close position</Button>}
        <Link className="text-xs underline" href={`/decisions/${p.decision_id}`}>Why did the agent buy this?</Link>
        <Link className="text-xs underline" href={`/tokens/${encodeURIComponent(key)}`}>Token page</Link>
      </div>
    </CardContent></Card>
  );
}

type Sort = "NEWEST" | "PNL" | "SIZE";

function Summary({ rows }: { rows: Position[] }) {
  const open = rows.filter((p) => p.status === "OPEN");
  const closed = rows.filter((p) => p.status === "CLOSED");
  const unreal = open.reduce((a, p) => a + p.unrealized_pnl_usdc, 0);
  const exposure = open.reduce((a, p) => a + p.cost_basis_usdc, 0);
  const realized = rows.reduce((a, p) => a + p.realized_pnl_usdc, 0);
  const wins = closed.filter((p) => p.realized_pnl_usdc > 0).length;
  return (
    <Card><CardContent className="grid grid-cols-2 gap-3 pt-4 text-sm md:grid-cols-5">
      <Field label="Open positions">{open.length}</Field>
      <Field label="Exposure">{usd(exposure)}</Field>
      <Field label="Unrealized P&L"><span className={pnlClass(unreal)}>{signedUsd(unreal)}</span></Field>
      <Field label="Realized P&L"><span className={pnlClass(realized)}>{signedUsd(realized)}</span></Field>
      <Field label="Win rate (closed)">{closed.length ? `${Math.round((wins / closed.length) * 100)}% of ${closed.length}` : "—"}</Field>
    </CardContent></Card>
  );
}

export function Positions() {
  const { user } = useAuth();
  const [picked, setPicked] = useState<HistoryMode | null>(null);
  const mode: HistoryMode = picked ?? (user?.mode === "LIVE" ? "LIVE" : "PAPER");
  const res = useApi<Position[]>(`/positions?mode=${mode}`, { refreshOn: ["POSITION_OPENED", "POSITION_UPDATED", "POSITION_CLOSED"], intervalMs: 10000 });
  const { toast, suppressAuto } = useToast();
  const [tab, setTab] = useState<"OPEN" | "CLOSED">("OPEN");
  const [sort, setSort] = useState<Sort>("NEWEST");
  const [target, setTarget] = useState<Position | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const rows = useMemo(() => {
    const list = (res.data ?? []).filter((p) => p.status === tab);
    const by: Record<Sort, (a: Position, b: Position) => number> = {
      NEWEST: (a, b) => new Date((tab === "OPEN" ? b.opened_at : b.closed_at ?? b.opened_at)).getTime() - new Date((tab === "OPEN" ? a.opened_at : a.closed_at ?? a.opened_at)).getTime(),
      PNL: (a, b) => (b.realized_pnl_usdc + b.unrealized_pnl_usdc) - (a.realized_pnl_usdc + a.unrealized_pnl_usdc),
      SIZE: (a, b) => b.cost_basis_usdc - a.cost_basis_usdc,
    };
    return [...list].sort(by[sort]);
  }, [res.data, tab, sort]);
  if (res.loading && !res.data) return <Loading />;
  if (!res.data) return res.error ? <ErrorState error={res.error} onRetry={() => void res.reload()} /> : null;
  const counts = { OPEN: res.data.filter((p) => p.status === "OPEN").length, CLOSED: res.data.filter((p) => p.status === "CLOSED").length };

  async function close() {
    if (!target) return;
    const t = target;
    const name = t.symbol ?? shortAddr(t.token_address);
    setBusy(true); setError(null);
    let queued: { command_id?: string } | null = null;
    try { queued = await api<{ command_id?: string }>(`/agent/close/${t.id}`, { method: "POST" }); }
    catch (e) { setError(toApiError(e)); setBusy(false); return; }
    setTarget(null); setBusy(false);
    const id = `close-${t.id}`;
    toast({ id, kind: "loading", title: `Closing ${name}`, description: "Close requested; your runner is selling at the current market price." });
    if (queued.command_id) {
      suppressAuto(60_000);
      const out = await waitForCommand(queued.command_id, { timeoutMs: 60_000 });
      if (out.status === "FAILED") toast({ id, kind: "error", title: `Could not close ${name}`, description: out.detail });
      else if (out.status === "DONE") toast({ id, kind: "success", title: `Position closed: ${name}`, description: "Manually closed.", href: "/positions", linkLabel: "Positions" });
      else toast({ id, kind: "warning", title: `Still waiting to close ${name}`, description: "The runner has not confirmed yet." });
      suppressAuto(3_000);
    }
    await res.reload();
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <ModeTabs value={mode} onChange={(m) => { setPicked(m); setTab("OPEN"); }} current={user?.mode === "LIVE" ? "LIVE" : "PAPER"} />
        <p className="text-xs text-muted-foreground">{mode === "PAPER" ? "Paper positions use virtual money." : "Live positions use real funds."}</p>
      </div>
      <Summary rows={res.data} />
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex gap-2" role="group" aria-label="Position status">
          {(["OPEN", "CLOSED"] as const).map((t) => <Button key={t} size="sm" variant={tab === t ? "default" : "outline"} aria-pressed={tab === t} onClick={() => setTab(t)}>{t === "OPEN" ? "Open" : "Closed"} ({counts[t]})</Button>)}
        </div>
        <div className="ml-auto flex items-center gap-1 text-xs" role="group" aria-label="Sort positions">
          <span className="text-muted-foreground">Sort</span>
          {([["NEWEST", tab === "OPEN" ? "Newest" : "Recently closed"], ["PNL", "P&L"], ["SIZE", "Size"]] as const).map(([k, label]) => (
            <Button key={k} size="sm" variant={sort === k ? "default" : "outline"} aria-pressed={sort === k} onClick={() => setSort(k)}>{label}</Button>))}
        </div>
      </div>
      {error && <ErrorState error={error} />}
      {rows.length === 0 ? <Empty>{tab === "OPEN" ? `No open ${mode.toLowerCase()} positions.` : `No closed ${mode.toLowerCase()} positions yet.`}</Empty> : rows.map((p) => <PositionCard key={p.id} p={p} onClose={setTarget} />)}
      <ConfirmDialog open={target !== null} onOpenChange={(o) => { if (!o) setTarget(null); }} busy={busy} destructive title="Close this position?"
        description="A close request is sent to your runner, which sells at the current market price using your slippage limits." confirmLabel="Close position" onConfirm={close} />
    </div>
  );
}
