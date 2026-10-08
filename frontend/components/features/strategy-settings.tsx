"use client";
import { useMemo, useState } from "react";
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

const TRACTION_WEIGHTS = [
  "momentum",
  "buyer_growth",
  "buy_sell_pressure",
  "liquidity_quality",
  "holder_distribution",
  "creator_behavior",
  "market_quality",
] as const;

const LIQUIDITY_WEIGHTS = [
  "liquidity_depth",
  "market_quality",
  "holder_quality",
  "activity",
  "momentum",
] as const;

type Cfg = {
  min_score?: number;
  watch_score?: number;
  weights?: Record<string, number>;
  exit?: { hard_stop_pct?: number; trailing_stop_pct?: number; stagnation_seconds?: number };
} & Record<string, unknown>;

/** Weight names come from the strategy's own saved config (or its defaults), so a new strategy needs no UI change. */
export function weightKeysFor(sid: string, cfgWeights?: Record<string, number>, metaWeights?: Record<string, number>): readonly string[] {
  const keys = Object.keys(cfgWeights ?? metaWeights ?? {});
  if (keys.length) return keys;
  return sid === "liquidity_trend" ? LIQUIDITY_WEIGHTS : TRACTION_WEIGHTS;
}

/** Weights, thresholds and exit rules of one strategy, saved as an immutable new version. Lives in Settings. */
export function StrategySettings() {
  const { user } = useAuth();
  const list = useApi<StrategyListItem[]>("/strategies");
  const [chosen, setChosen] = useState<string | null>(null);
  const [draft, setDraft] = useState<{ min: number; watch: number; hard: number; trail: number; stagn: number; weights: Record<string, number> } | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [saved, setSaved] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  // The strategy being edited: the one the user picked here, else the one that is running.
  const selectedId = chosen ?? list.data?.find((s) => s.active)?.id ?? list.data?.find((s) => s.enabled)?.id ?? list.data?.[0]?.id ?? "traction_momentum";
  const versions = useApi<StrategyVersion[]>(`/strategies/${selectedId}/versions`);
  const selectedMeta = useMemo(() => list.data?.find((s) => s.id === selectedId), [list.data, selectedId]);

  if (list.loading && !list.data) return <Loading />;
  if (list.error && !list.data) return <ErrorState error={list.error} onRetry={() => void list.reload()} />;
  if (versions.loading && !versions.data) return <Loading />;
  if (!versions.data) return versions.error ? <ErrorState error={versions.error} onRetry={() => void versions.reload()} /> : null;

  const active = versions.data[0];
  const cfg = (active?.config || {}) as Cfg;
  const keys = weightKeysFor(selectedId, cfg.weights, selectedMeta?.weights);
  const d = draft ?? {
    min: cfg.min_score ?? 25, watch: cfg.watch_score ?? 12, hard: cfg.exit?.hard_stop_pct ?? 20, trail: cfg.exit?.trailing_stop_pct ?? 20,
    stagn: Math.round(((cfg.exit?.stagnation_seconds ?? 1800) / 3600) * 100) / 100,
    weights: Object.fromEntries(keys.map((w) => [w, Number((cfg.weights?.[w] ?? 0) * 100)])) as Record<string, number>,
  };
  const wsum = Object.values(d.weights).reduce((a, b) => a + b, 0);
  const problem = d.watch > d.min ? "Watch score cannot exceed the qualification score."
    : wsum <= 0 ? "Weights must add up to more than zero."
    : d.hard <= 0 || d.hard > 90 ? "Hard stop must be between 0 and 90%."
    : !(d.stagn >= 0.1 && d.stagn <= 168) ? "Stagnation exit must be between 0.1 and 168 hours." : null;

  async function save() {
    setBusy(true); setError(null); setSaved(null);
    try {
      const next: Cfg = {
        ...cfg, min_score: d.min, watch_score: d.watch,
        weights: Object.fromEntries(keys.map((w) => [w, d.weights[w] ?? 0])),
        exit: { ...(cfg.exit ?? {}), hard_stop_pct: d.hard, trailing_stop_pct: d.trail, stagnation_seconds: Math.round(d.stagn * 3600) },
      };
      delete next.strategy_id; delete next.version;
      const r = await api<{ version: number }>(`/strategies/${selectedId}/versions`, { method: "POST", body: { config: next, confirm: user?.mode === "LIVE" } });
      setSaved(r.version); setDraft(null); await versions.reload();
    } catch (e) { setError(toApiError(e)); } finally { setBusy(false); }
  }
  const num_ = (v: string) => (v === "" ? NaN : Number(v));

  return (
    <div className="space-y-6">
      <Card><CardHeader><CardTitle>Strategy to configure</CardTitle></CardHeader><CardContent className="space-y-2 text-sm">
        <div className="flex flex-wrap gap-2" role="group" aria-label="Strategy">
          {(list.data ?? []).map((s) => (
            <Button key={s.id} size="sm" variant={selectedId === s.id ? "default" : "outline"} aria-pressed={selectedId === s.id}
              onClick={() => { setChosen(s.id); setDraft(null); setSaved(null); setError(null); }}>
              {s.name}{s.active ? " (running)" : ""}
            </Button>))}
        </div>
        <p className="text-muted-foreground">{selectedMeta?.description}</p>
        <p>Qualifies at score {num(cfg.min_score ?? 0, 0)}; WATCH from {num(cfg.watch_score ?? 0, 0)}. <Badge variant="solidPrimary">v{active.version} config</Badge> Weights are starting defaults, not claimed optimal.</p>
        {selectedMeta?.exit_profile && <p className="text-xs text-muted-foreground">Built-in exit profile: stop {selectedMeta.exit_profile.hard_stop_pct}% · first target +{selectedMeta.exit_profile.first_target_pct ?? "—"}% · trailing {selectedMeta.exit_profile.trailing_stop_pct}% · flat-trade exit after {selectedMeta.exit_profile.stagnation_hours}h</p>}
      </CardContent></Card>

      <Card><CardHeader><CardTitle>Create a new version</CardTitle></CardHeader><CardContent className="space-y-3">
        <p className="text-xs text-muted-foreground">Versions are immutable. Every trade decision records the version and full configuration that produced it. Saving restarts the agent in STOPPED state.</p>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          {([["min", "Qualify score"], ["watch", "Watch score"], ["hard", "Hard stop (%)"], ["trail", "Trailing stop (%)"], ["stagn", "Flat-trade exit after (hours)"]] as const).map(([k, label]) => (
            <div key={k} className="space-y-1.5"><Label htmlFor={`s-${k}`}>{label}</Label>
              <Input id={`s-${k}`} type="number" value={Number.isNaN(d[k]) ? "" : d[k]} onChange={(e) => setDraft({ ...d, [k]: num_(e.target.value) })} /></div>))}
        </div>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {keys.map((w) => (
            <div key={w} className="space-y-1.5"><Label htmlFor={`w-${w}`}>{w.replaceAll("_", " ")} (%)</Label>
              <Input id={`w-${w}`} type="number" value={Number.isNaN(d.weights[w]) ? "" : Math.round(d.weights[w] * 10) / 10}
                onChange={(e) => setDraft({ ...d, weights: { ...d.weights, [w]: num_(e.target.value) } })} /></div>))}
        </div>
        <p className="text-xs text-muted-foreground">Weights are normalised to 100% on save (currently {num(wsum, 0)}).</p>
        {problem && <Alert variant="warning">{problem}</Alert>}
        {error && <ErrorState error={error} />}
        {saved && <Alert variant="success">Saved as version {saved}.</Alert>}
        <Button disabled={!!problem || busy || draft === null} onClick={() => void save()}>Save as new version</Button>
      </CardContent></Card>

      <Card><CardHeader><CardTitle>Version history</CardTitle></CardHeader><CardContent className="space-y-1 text-sm">
        {versions.data.map((v) => (
          <div key={`${v.scope}-${v.version}`} className="flex justify-between"><span>v{v.version} ({v.scope})</span>
            <span className="text-xs text-muted-foreground">{new Date(v.created_at).toLocaleString()}</span></div>))}
      </CardContent></Card>
    </div>
  );
}
