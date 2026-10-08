"use client";
import { useState } from "react";
import { ModeTabs, type HistoryMode } from "@/components/mode-tabs";
import { Empty, ErrorState, Loading } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useApi } from "@/hooks/use-api";
import { useAuth } from "@/lib/auth";
import type { AuditLog } from "@/types/api";

interface Row { id: string; type: string; at: string; correlation_id: string; mode?: string | null; payload: Record<string, unknown> }
const summary = (p: Record<string, unknown>) => Object.entries(p).slice(0, 3).map(([k, v]) => `${k}: ${typeof v === "object" ? "…" : String(v)}`).join(" · ");

export function Activity() {
  const { user } = useAuth();
  const [tab, setTab] = useState<"events" | "audit">("events");
  const [type, setType] = useState("");
  const [picked, setPicked] = useState<HistoryMode | null>(null);
  const current: HistoryMode = user?.mode === "LIVE" ? "LIVE" : "PAPER";
  const mode = picked ?? current;
  // Trade events (orders, positions, exits) belong to one mode and are filtered by it; system events (agent state,
  // errors, emergency stop) belong to no mode and always show. Live push events carry no mode, so this list is
  // refreshed from the server when they arrive instead of being merged in.
  const rest = useApi<Row[]>(`/activity?limit=100&mode=${mode}${type ? `&type=${type}` : ""}`, { refreshOn: ["DECISION_RECORDED", "ORDER_FILLED", "POSITION_OPENED", "POSITION_CLOSED", "RISK_ALERT", "AGENT_ERROR"] });
  const audit = useApi<AuditLog[]>(tab === "audit" ? "/audit-logs" : null);
  const merged: Row[] = rest.data ?? [];

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        {tab === "events" && <ModeTabs value={mode} onChange={setPicked} current={current} />}
        {(["events", "audit"] as const).map((t) => <Button key={t} size="sm" variant={tab === t ? "default" : "outline"} aria-pressed={tab === t} onClick={() => setTab(t)}>{t === "events" ? "Events" : "Audit log"}</Button>)}
        {tab === "events" && (<label className="ml-auto flex items-center gap-2 text-xs tracking-[0.03em]">Type
          <select className="h-10 rounded-md border bg-background px-2.5 text-sm" value={type} onChange={(e) => setType(e.target.value)} aria-label="Event type">
            <option value="">All</option>{["DECISION_RECORDED", "ORDER_FILLED", "ORDER_FAILED", "POSITION_OPENED", "POSITION_CLOSED", "TAKE_PROFIT_TRIGGERED", "STOP_LOSS_TRIGGERED", "TRAILING_STOP_TRIGGERED", "EMERGENCY_STOP_CHANGED", "RISK_ALERT", "AGENT_ERROR"].map((t) => <option key={t} value={t}>{t}</option>)}
          </select></label>)}
      </div>
      {tab === "events" ? (rest.loading && !rest.data ? <Loading /> : rest.error && !rest.data ? <ErrorState error={rest.error} onRetry={() => void rest.reload()} /> :
        merged.length === 0 ? <Empty>{`No ${mode.toLowerCase()} activity yet.`}</Empty> : (<Card><CardContent className="divide-y p-0">{merged.map((e) => (
          <div key={e.id} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-5 py-3.5 text-sm transition-colors duration-200 hover:bg-muted/40"><span className="font-mono text-xs font-medium">{e.type}</span><span className="text-xs text-muted-foreground">{new Date(e.at).toLocaleTimeString()}</span>
            {e.correlation_id ? null : <span className="rounded-sm border px-1.5 font-mono text-[10px] tracking-[0.08em] text-muted-foreground">system</span>}<span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">{summary(e.payload)}</span></div>))}</CardContent></Card>))
        : (audit.loading && !audit.data ? <Loading /> : audit.error ? <ErrorState error={audit.error} /> : (audit.data ?? []).length === 0 ? <Empty>No audit entries.</Empty> :
          <Card><CardContent className="divide-y p-0">{(audit.data ?? []).map((a, i) => (<div key={`${a.at}-${i}`} className="px-5 py-3.5 text-sm transition-colors duration-200 hover:bg-muted/40"><span className="font-mono text-xs font-medium">{a.action}</span> <span className="text-xs text-muted-foreground">{a.actor} · {new Date(a.at).toLocaleString()}</span></div>))}</CardContent></Card>)}
    </div>
  );
}
