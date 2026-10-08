"use client";
import { useState } from "react";
import Link from "next/link";
import { Settings as SettingsIcon } from "lucide-react";
import { ErrorState, Loading } from "@/components/states";
import { useToast } from "@/components/toast";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useApi } from "@/hooks/use-api";
import { api, toApiError, type ApiError } from "@/lib/api";
import type { PerformanceStats, StrategyListItem } from "@/types/api";

/** Pick which strategy the agent runs. Changing weights, thresholds and exit rules is under Settings. */
export function Strategies() {
  const { toast } = useToast();
  const list = useApi<StrategyListItem[]>("/strategies");
  const perf = useApi<PerformanceStats>("/portfolio/performance", { intervalMs: 60000 });
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);

  if (list.loading && !list.data) return <Loading />;
  if (list.error && !list.data) return <ErrorState error={list.error} onRetry={() => void list.reload()} />;
  const stats = new Map((perf.data?.by_strategy ?? []).map((g) => [g.key, g]));

  async function activate(sid: string, name: string) {
    setBusy(true); setError(null);
    try {
      await api(`/strategies/${sid}/activate`, { method: "PUT" });
      toast({ id: "strategy-activate", kind: "success", title: `${name} is now the active strategy`, description: "Open positions keep the exit rules of the strategy that opened them." });
      await list.reload();
    } catch (e) { setError(toApiError(e)); } finally { setBusy(false); }
  }

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">The agent runs <strong>one strategy at a time</strong>. Each comes with its own exit rules: a fresh launch needs minutes, an established token needs hours. Open positions keep the rules of the strategy that opened them.</p>
      {error && <ErrorState error={error} />}
      {(list.data ?? []).map((s) => {
        const g = stats.get(s.id);
        return (
          <Card key={s.id} className={s.active ? "border-primary" : undefined}><CardContent className="space-y-2 p-4 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold">{s.name}</span>
              {s.active && <Badge variant="solidPrimary">RUNNING</Badge>}
              {s.profile && <Badge>{s.profile === "launch" ? "fresh launches" : "established tokens"}</Badge>}
              <span className="ml-auto flex gap-2">
                {!s.active && <Button size="sm" disabled={busy} onClick={() => void activate(s.id, s.name)}>Run this strategy</Button>}
                {s.enabled && !s.active && <Button size="sm" variant="outline" disabled={busy} onClick={async () => { setBusy(true); try { await api(`/strategies/${s.id}/enabled`, { method: "PUT", body: { enabled: false } }); await list.reload(); } catch (e) { setError(toApiError(e)); } finally { setBusy(false); } }}>Disable</Button>}
              </span>
            </div>
            <p className="text-muted-foreground">{s.description}</p>
            {s.best_for && <p className="text-xs">Best for: {s.best_for}</p>}
            {s.exit_profile && <p className="text-xs text-muted-foreground">Default exits: stop {s.exit_profile.hard_stop_pct}% · first target +{s.exit_profile.first_target_pct ?? "—"}% · trailing {s.exit_profile.trailing_stop_pct}% · flat-trade exit after {s.exit_profile.stagnation_hours}h</p>}
            <p className="text-xs text-muted-foreground">{g ? `Your results: ${g.count} closed trade${g.count === 1 ? "" : "s"}, ${g.win_rate_pct}% won, ${g.pnl_usdc >= 0 ? "+" : ""}$${g.pnl_usdc.toFixed(2)} (current mode)` : "No closed trades with this strategy yet in the current mode."}</p>
          </CardContent></Card>);
      })}
      {!list.data?.some((s) => s.id === "liquidity_trend") && <Alert variant="warning">Liquidity Trend is not in the control-plane catalog yet. Redeploy the control plane with the latest backend so it is seeded, then refresh.</Alert>}
      <Link href="/settings?tab=strategy" className="inline-flex items-center gap-1.5 text-sm underline"><SettingsIcon className="h-4 w-4" aria-hidden />Edit weights, thresholds and exit rules in Settings</Link>
    </div>
  );
}
