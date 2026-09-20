"use client";
import { useState } from "react";
import { ErrorState, Loading } from "@/components/states";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { useApi } from "@/hooks/use-api";
import { api, toApiError, type ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { num } from "@/lib/format";
import type { StrategyListItem, StrategyVersion } from "@/types/api";

const WEIGHTS = ["momentum", "buyer_growth", "buy_sell_pressure", "liquidity_quality", "holder_distribution", "creator_behavior", "market_quality"] as const;
type Cfg = { min_score?: number; watch_score?: number; weights?: Record<string, number>; exit?: { hard_stop_pct?: number; trailing_stop_pct?: number; stagnation_seconds?: number } } & Record<string, unknown>;

export function Strategies() {
  const { user } = useAuth();
  const list = useApi<StrategyListItem[]>("/strategies");
  const versions = useApi<StrategyVersion[]>("/strategies/traction_momentum/versions");
  const [draft, setDraft] = useState<{ min: number; watch: number; hard: number; trail: number; weights: Record<string, number> } | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [saved, setSaved] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  if (versions.loading && !versions.data) return <Loading />;
  if (!versions.data) return versions.error ? <ErrorState error={versions.error} onRetry={() => void versions.reload()} /> : null;
  const active = versions.data[0], cfg = active.config as Cfg;
  const d = draft ?? { min: cfg.min_score ?? 70, watch: cfg.watch_score ?? 50, hard: cfg.exit?.hard_stop_pct ?? 20, trail: cfg.exit?.trailing_stop_pct ?? 20,
    weights: Object.fromEntries(WEIGHTS.map((w) => [w, Number((cfg.weights?.[w] ?? 0) * 100)])) as Record<string, number> };
  const wsum = Object.values(d.weights).reduce((a, b) => a + b, 0);
  const problem = d.watch > d.min ? "Watch score cannot exceed the qualification score." : wsum <= 0 ? "Weights must add up to more than zero." : d.hard <= 0 || d.hard > 90 ? "Hard stop must be between 0 and 90%." : null;

  async function toggle(enabled: boolean) {
    setBusy(true); setError(null);
    try { await api("/strategies/traction_momentum/enabled", { method: "PUT", body: { enabled } }); await list.reload(); } catch (e) { setError(toApiError(e)); } finally { setBusy(false); }
  }
  async function save() {
    setBusy(true); setError(null); setSaved(null);
    try {
      const next: Cfg = { ...cfg, min_score: d.min, watch_score: d.watch, weights: Object.fromEntries(WEIGHTS.map((w) => [w, d.weights[w]])),
        exit: { ...(cfg.exit ?? {}), hard_stop_pct: d.hard, trailing_stop_pct: d.trail } };
      delete next.strategy_id; delete next.version;
      const r = await api<{ version: number }>("/strategies/traction_momentum/versions", { method: "POST", body: { config: next, confirm: user?.mode === "LIVE" } });
      setSaved(r.version); setDraft(null); await versions.reload();
    } catch (e) { setError(toApiError(e)); } finally { setBusy(false); }
  }
  const num_ = (v: string) => (v === "" ? NaN : Number(v));
  return (
    <div className="space-y-4">
      <Card><CardHeader><CardTitle>{list.data?.[0]?.name ?? "Traction Momentum"} <Badge variant="solidPrimary">v{active.version} active</Badge></CardTitle></CardHeader>
        <CardContent className="space-y-1 text-sm"><p className="text-muted-foreground">{list.data?.[0]?.description}</p>
          <div className="flex flex-wrap items-center gap-2"><Badge variant={list.data?.[0]?.enabled ? "success" : "warning"}>{list.data?.[0]?.enabled ? "ENABLED" : "DISABLED"}</Badge>
            <Button size="sm" variant="outline" disabled={busy || !list.data} onClick={() => void toggle(!list.data?.[0]?.enabled)}>{list.data?.[0]?.enabled ? "Disable strategy" : "Enable strategy"}</Button>
            <span className="text-xs text-muted-foreground">A disabled strategy opens no new positions; your runner keeps managing open ones.</span></div>
          <p>Qualifies at score {num(cfg.min_score ?? 0, 0)}; WATCH from {num(cfg.watch_score ?? 0, 0)}. Weights are initial defaults, not claimed optimal.</p></CardContent></Card>
      <Card><CardHeader><CardTitle>Create a new version</CardTitle></CardHeader><CardContent className="space-y-3">
        <p className="text-xs text-muted-foreground">Versions are immutable. Every trade decision records the version and full configuration that produced it. Saving restarts the agent in STOPPED state.</p>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {([["min", "Qualify score"], ["watch", "Watch score"], ["hard", "Hard stop (%)"], ["trail", "Trailing stop (%)"]] as const).map(([k, label]) => (
            <div key={k} className="space-y-1"><Label htmlFor={`s-${k}`}>{label}</Label><Input id={`s-${k}`} type="number" value={Number.isNaN(d[k]) ? "" : d[k]} onChange={(e) => setDraft({ ...d, [k]: num_(e.target.value) })} /></div>))}
        </div>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {WEIGHTS.map((w) => (<div key={w} className="space-y-1"><Label htmlFor={`w-${w}`}>{w.replaceAll("_", " ")} (%)</Label>
            <Input id={`w-${w}`} type="number" value={Number.isNaN(d.weights[w]) ? "" : Math.round(d.weights[w] * 10) / 10} onChange={(e) => setDraft({ ...d, weights: { ...d.weights, [w]: num_(e.target.value) } })} /></div>))}
        </div>
        <p className="text-xs text-muted-foreground">Weights are normalised to 100% on save (currently {num(wsum, 0)}).</p>
        {problem && <Alert variant="warning">{problem}</Alert>}{error && <ErrorState error={error} />}{saved && <Alert variant="success">Saved as version {saved}.</Alert>}
        <Button disabled={!!problem || busy || draft === null} onClick={() => void save()}>Save as new version</Button></CardContent></Card>
      <Card><CardHeader><CardTitle>Version history</CardTitle></CardHeader><CardContent className="space-y-1 text-sm">
        {versions.data.map((v) => <div key={`${v.scope}-${v.version}`} className="flex justify-between"><span>v{v.version} ({v.scope})</span><span className="text-xs text-muted-foreground">{new Date(v.created_at).toLocaleString()}</span></div>)}</CardContent></Card>
    </div>
  );
}
