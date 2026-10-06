"use client";
import { useEffect, useMemo, useState } from "react";
import { ErrorState, Loading } from "@/components/states";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { useApi } from "@/hooks/use-api";
import { api, toApiError, type ApiError } from "@/lib/api";
import { useToast } from "@/components/toast";
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
function weightKeysFor(sid: string, cfgWeights?: Record<string, number>, metaWeights?: Record<string, number>): readonly string[] {
  const keys = Object.keys(cfgWeights ?? metaWeights ?? {});
  if (keys.length) return keys;
  return sid === "liquidity_trend" ? LIQUIDITY_WEIGHTS : TRACTION_WEIGHTS;
}

export function Strategies() {
  const { user } = useAuth();
  const { toast } = useToast();
  const list = useApi<StrategyListItem[]>("/strategies");
  const [selectedId, setSelectedId] = useState<string>("traction_momentum");
  const [draft, setDraft] = useState<{
    min: number;
    watch: number;
    hard: number;
    trail: number;
    stagn: number;
    weights: Record<string, number>;
  } | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [saved, setSaved] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  // Prefer active strategy from API once list loads.
  useEffect(() => {
    if (!list.data?.length) return;
    const active = list.data.find((s) => s.active) || list.data.find((s) => s.enabled) || list.data[0];
    if (active && active.id !== selectedId) {
      setSelectedId(active.id);
      setDraft(null);
      setSaved(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [list.data]);

  const versions = useApi<StrategyVersion[]>(
    selectedId ? `/strategies/${selectedId}/versions` : "/strategies/traction_momentum/versions",
  );

  const selectedMeta = useMemo(
    () => list.data?.find((s) => s.id === selectedId) || list.data?.[0],
    [list.data, selectedId],
  );

  if (list.loading && !list.data) return <Loading />;
  if (list.error && !list.data) return <ErrorState error={list.error} onRetry={() => void list.reload()} />;
  if (versions.loading && !versions.data) return <Loading />;
  if (!versions.data) {
    return versions.error ? <ErrorState error={versions.error} onRetry={() => void versions.reload()} /> : null;
  }

  const active = versions.data[0];
  const cfg = (active?.config || {}) as Cfg;
  const keys = weightKeysFor(selectedId, cfg.weights, selectedMeta?.weights);
  const d =
    draft ??
    {
      min: cfg.min_score ?? 25,
      watch: cfg.watch_score ?? 12,
      hard: cfg.exit?.hard_stop_pct ?? 20,
      trail: cfg.exit?.trailing_stop_pct ?? 20,
      stagn: Math.round(((cfg.exit?.stagnation_seconds ?? 1800) / 3600) * 100) / 100,
      weights: Object.fromEntries(
        keys.map((w) => [w, Number((cfg.weights?.[w] ?? 0) * 100)]),
      ) as Record<string, number>,
    };
  const wsum = Object.values(d.weights).reduce((a, b) => a + b, 0);
  const problem =
    d.watch > d.min
      ? "Watch score cannot exceed the qualification score."
      : wsum <= 0
        ? "Weights must add up to more than zero."
        : d.hard <= 0 || d.hard > 90
          ? "Hard stop must be between 0 and 90%."
          : !(d.stagn >= 0.1 && d.stagn <= 168)
            ? "Stagnation exit must be between 0.1 and 168 hours."
            : null;

  async function toggle(sid: string, enabled: boolean) {
    setBusy(true);
    setError(null);
    try {
      await api(`/strategies/${sid}/enabled`, { method: "PUT", body: { enabled } });
      await list.reload();
    } catch (e) {
      setError(toApiError(e));
    } finally {
      setBusy(false);
    }
  }

  async function activate(sid: string, name: string) {
    setBusy(true);
    setError(null);
    try {
      await api(`/strategies/${sid}/activate`, { method: "PUT" });
      toast({ id: "strategy-activate", kind: "success", title: `${name} is now the active strategy`, description: "Open positions keep the exit rules of the strategy that opened them." });
      await list.reload();
    } catch (e) {
      setError(toApiError(e));
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    setBusy(true);
    setError(null);
    setSaved(null);
    try {
      const next: Cfg = {
        ...cfg,
        min_score: d.min,
        watch_score: d.watch,
        weights: Object.fromEntries(keys.map((w) => [w, d.weights[w] ?? 0])),
        exit: { ...(cfg.exit ?? {}), hard_stop_pct: d.hard, trailing_stop_pct: d.trail, stagnation_seconds: Math.round(d.stagn * 3600) },
      };
      delete next.strategy_id;
      delete next.version;
      const r = await api<{ version: number }>(`/strategies/${selectedId}/versions`, {
        method: "POST",
        body: { config: next, confirm: user?.mode === "LIVE" },
      });
      setSaved(r.version);
      setDraft(null);
      await versions.reload();
    } catch (e) {
      setError(toApiError(e));
    } finally {
      setBusy(false);
    }
  }

  const num_ = (v: string) => (v === "" ? NaN : Number(v));

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>Strategies</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <p className="text-muted-foreground">
            The agent runs <strong>one strategy at a time</strong>: choose it with <em>Run this strategy</em>. Each strategy comes with
            its own exit rules (stop, targets, how long a flat trade is given), because a fresh launch needs minutes and an
            established token needs hours. Open positions keep the rules of the strategy that opened them.
          </p>
          {(list.data || []).map((s) => (
            <div
              key={s.id}
              className={`rounded-lg border p-3 space-y-2 ${selectedId === s.id ? "border-primary" : ""}`}
            >
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  className="font-medium underline-offset-2 hover:underline"
                  onClick={() => {
                    setSelectedId(s.id);
                    setDraft(null);
                    setSaved(null);
                  }}
                >
                  {s.name}
                </button>
                {s.active && <Badge variant="solidPrimary">RUNNING</Badge>}
                {s.profile && <Badge>{s.profile === "launch" ? "fresh launches" : "established tokens"}</Badge>}
                <Badge variant={s.enabled ? "success" : "warning"}>
                  {s.enabled ? "ENABLED" : "DISABLED"}
                </Badge>
                {!s.active && (
                  <Button size="sm" disabled={busy} onClick={() => void activate(s.id, s.name)}>
                    Run this strategy
                  </Button>
                )}
                {s.enabled && !s.active && (
                  <Button size="sm" variant="outline" disabled={busy} onClick={() => void toggle(s.id, false)}>
                    Disable
                  </Button>
                )}
              </div>
              <p className="text-xs text-muted-foreground">{s.description}</p>
              {s.best_for && <p className="text-xs">Best for: {s.best_for}</p>}
              {s.exit_profile && (
                <p className="text-xs text-muted-foreground">
                  Default exits: stop {s.exit_profile.hard_stop_pct}% · first target +{s.exit_profile.first_target_pct ?? "—"}% ·
                  trailing {s.exit_profile.trailing_stop_pct}% · flat-trade exit after {s.exit_profile.stagnation_hours}h
                </p>
              )}
            </div>
          ))}
          {!list.data?.some((s) => s.id === "liquidity_trend") && (
            <Alert variant="warning">
              Liquidity Trend is not in the control-plane catalog yet. Redeploy the Render control
              plane with the latest backend so it is seeded, then refresh this page.
            </Alert>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>
            {selectedMeta?.name ?? selectedId}{" "}
            <Badge variant="solidPrimary">v{active.version} config</Badge>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-1 text-sm">
          <p className="text-muted-foreground">{selectedMeta?.description}</p>
          <p>
            Qualifies at score {num(cfg.min_score ?? 0, 0)}; WATCH from {num(cfg.watch_score ?? 0, 0)}.
            Weights are initial defaults, not claimed optimal.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Create a new version</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-xs text-muted-foreground">
            Versions are immutable. Every trade decision records the version and full configuration
            that produced it. Saving restarts the agent in STOPPED state.
          </p>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            {(
              [
                ["min", "Qualify score"],
                ["watch", "Watch score"],
                ["hard", "Hard stop (%)"],
                ["trail", "Trailing stop (%)"],
                ["stagn", "Flat-trade exit after (hours)"],
              ] as const
            ).map(([k, label]) => (
              <div key={k} className="space-y-1">
                <Label htmlFor={`s-${k}`}>{label}</Label>
                <Input
                  id={`s-${k}`}
                  type="number"
                  value={Number.isNaN(d[k]) ? "" : d[k]}
                  onChange={(e) => setDraft({ ...d, [k]: num_(e.target.value) })}
                />
              </div>
            ))}
          </div>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {keys.map((w) => (
              <div key={w} className="space-y-1">
                <Label htmlFor={`w-${w}`}>{w.replaceAll("_", " ")} (%)</Label>
                <Input
                  id={`w-${w}`}
                  type="number"
                  value={Number.isNaN(d.weights[w]) ? "" : Math.round(d.weights[w] * 10) / 10}
                  onChange={(e) =>
                    setDraft({
                      ...d,
                      weights: { ...d.weights, [w]: num_(e.target.value) },
                    })
                  }
                />
              </div>
            ))}
          </div>
          <p className="text-xs text-muted-foreground">
            Weights are normalised to 100% on save (currently {num(wsum, 0)}).
          </p>
          {problem && <Alert variant="warning">{problem}</Alert>}
          {error && <ErrorState error={error} />}
          {saved && <Alert variant="success">Saved as version {saved}.</Alert>}
          <Button disabled={!!problem || busy || draft === null} onClick={() => void save()}>
            Save as new version
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Version history</CardTitle>
        </CardHeader>
        <CardContent className="space-y-1 text-sm">
          {versions.data.map((v) => (
            <div key={`${v.scope}-${v.version}`} className="flex justify-between">
              <span>
                v{v.version} ({v.scope})
              </span>
              <span className="text-xs text-muted-foreground">
                {new Date(v.created_at).toLocaleString()}
              </span>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
