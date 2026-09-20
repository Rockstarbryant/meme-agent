"use client";
import { useState } from "react";
import Link from "next/link";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Empty, ErrorState, Loading } from "@/components/states";
import { useApi } from "@/hooks/use-api";
import { api, toApiError, type ApiError } from "@/lib/api";
import { pct, pnlClass, price, shortAddr, signedUsd, usd } from "@/lib/format";
import type { Position } from "@/types/api";

export function PositionCard({ p, onClose }: { p: Position; onClose?: (p: Position) => void }) {
  const total = p.realized_pnl_usdc + p.unrealized_pnl_usdc;
  return (
    <Card><CardContent className="space-y-2 pt-4">
      <div className="flex flex-wrap items-center gap-2"><span className="font-semibold">{p.symbol ?? shortAddr(p.token_address)}</span>
        <Badge variant={p.mode === "LIVE" ? "solidDestructive" : "solidPrimary"}>{p.mode}</Badge><Badge>{p.status}</Badge>
        {p.exit_reason && <Badge variant="warning">{p.exit_reason}</Badge>}<Link className="ml-auto text-xs underline" href={`/decisions/${p.decision_id}`}>why?</Link></div>
      <dl className="grid grid-cols-2 gap-2 text-sm md:grid-cols-5">
        <div><dt className="text-xs text-muted-foreground">Entry</dt><dd>{price(p.entry_price)}</dd></div>
        <div><dt className="text-xs text-muted-foreground">Now</dt><dd>{price(p.last_price)} <span className={pnlClass(p.gain_pct)}>{pct(p.gain_pct)}</span></dd></div>
        <div><dt className="text-xs text-muted-foreground">Cost basis</dt><dd>{usd(p.cost_basis_usdc)}</dd></div>
        <div><dt className="text-xs text-muted-foreground">P&L</dt><dd className={pnlClass(total)}>{signedUsd(total)}</dd></div>
        <div><dt className="text-xs text-muted-foreground">Take-profit tiers hit</dt><dd>{p.tiers_hit.length ? p.tiers_hit.map((t) => t + 1).join(", ") : "none"}</dd></div>
      </dl>
      {p.status === "OPEN" && onClose && <Button size="sm" variant="outline" onClick={() => onClose(p)}>Close position</Button>}
    </CardContent></Card>
  );
}

export function Positions() {
  const res = useApi<Position[]>("/positions", { refreshOn: ["POSITION_OPENED", "POSITION_UPDATED", "POSITION_CLOSED"], intervalMs: 10000 });
  const [tab, setTab] = useState<"OPEN" | "CLOSED">("OPEN");
  const [target, setTarget] = useState<Position | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  if (res.loading && !res.data) return <Loading />;
  if (!res.data) return res.error ? <ErrorState error={res.error} onRetry={() => void res.reload()} /> : null;
  const rows = res.data.filter((p) => p.status === tab);
  async function close() {
    if (!target) return;
    setBusy(true); setError(null);
    try { await api(`/agent/close/${target.id}`, { method: "POST" }); setTarget(null); setNotice("Close requested. Your Local Runner will execute it within seconds."); await res.reload(); }
    catch (e) { setError(toApiError(e)); } finally { setBusy(false); }
  }
  return (
    <div className="space-y-3">
      <div className="flex gap-2" role="group" aria-label="Position status">
        {(["OPEN", "CLOSED"] as const).map((t) => <Button key={t} size="sm" variant={tab === t ? "default" : "outline"} aria-pressed={tab === t} onClick={() => setTab(t)}>{t === "OPEN" ? "Open" : "Closed"}</Button>)}
      </div>
      {error && <ErrorState error={error} />}
      {notice && <Alert variant="success">{notice}</Alert>}
      {rows.length === 0 ? <Empty>{tab === "OPEN" ? "No open positions." : "No closed positions yet."}</Empty> : rows.map((p) => <PositionCard key={p.id} p={p} onClose={setTarget} />)}
      <ConfirmDialog open={target !== null} onOpenChange={(o) => { if (!o) setTarget(null); }} busy={busy} destructive title="Close this position?"
        description="A close request is sent to your Local Runner, which sells at the current market price using your slippage limits." confirmLabel="Close position" onConfirm={close} />
    </div>
  );
}
