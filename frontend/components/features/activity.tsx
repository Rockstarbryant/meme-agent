"use client";
import { useState } from "react";
import { Empty, ErrorState, Loading } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useApi } from "@/hooks/use-api";
import { useEvents } from "@/lib/events";
import type { AuditLog, StreamEvent } from "@/types/api";

interface Row { id: string; type: string; at: string; correlation_id: string; payload: Record<string, unknown> }
const summary = (p: Record<string, unknown>) => Object.entries(p).slice(0, 3).map(([k, v]) => `${k}: ${typeof v === "object" ? "…" : String(v)}`).join(" · ");

export function Activity() {
  const [tab, setTab] = useState<"events" | "audit">("events");
  const [type, setType] = useState("");
  const { events: live } = useEvents();
  const rest = useApi<Row[]>(`/activity?limit=100${type ? `&type=${type}` : ""}`, { refreshOn: ["DECISION_RECORDED", "ORDER_FILLED", "POSITION_CLOSED", "RISK_ALERT", "AGENT_ERROR"] });
  const audit = useApi<AuditLog[]>(tab === "audit" ? "/audit-logs" : null);

  const merged: Row[] = (() => {
    const seen = new Set<string>(), out: Row[] = [];
    const liveRows = live.filter((e: StreamEvent) => e.type !== "POSITION_UPDATED" && e.type !== "TOKEN_ACTIVITY_UPDATED" && (!type || e.type === type));
    for (const e of [...liveRows, ...(rest.data ?? [])]) if (!seen.has(e.id)) { seen.add(e.id); out.push(e); }
    return out.slice(0, 150);
  })();

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        {(["events", "audit"] as const).map((t) => <Button key={t} size="sm" variant={tab === t ? "default" : "outline"} aria-pressed={tab === t} onClick={() => setTab(t)}>{t === "events" ? "Events" : "Audit log"}</Button>)}
        {tab === "events" && (<label className="ml-auto flex items-center gap-2 text-xs">Type
          <select className="h-8 rounded-md border bg-background px-2" value={type} onChange={(e) => setType(e.target.value)} aria-label="Event type">
            <option value="">All</option>{["DECISION_RECORDED", "ORDER_FILLED", "ORDER_FAILED", "POSITION_OPENED", "POSITION_CLOSED", "TAKE_PROFIT_TRIGGERED", "STOP_LOSS_TRIGGERED", "TRAILING_STOP_TRIGGERED", "EMERGENCY_STOP_CHANGED", "RISK_ALERT", "AGENT_ERROR"].map((t) => <option key={t} value={t}>{t}</option>)}
          </select></label>)}
      </div>
      {tab === "events" ? (rest.loading && !rest.data ? <Loading /> : rest.error && !rest.data ? <ErrorState error={rest.error} onRetry={() => void rest.reload()} /> :
        merged.length === 0 ? <Empty>No activity yet.</Empty> : (<Card><CardContent className="divide-y p-0">{merged.map((e) => (
          <div key={e.id} className="flex flex-wrap items-baseline gap-2 p-3 text-sm"><span className="font-mono text-xs">{e.type}</span><span className="text-xs text-muted-foreground">{new Date(e.at).toLocaleTimeString()}</span>
            <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">{summary(e.payload)}</span></div>))}</CardContent></Card>))
        : (audit.loading && !audit.data ? <Loading /> : audit.error ? <ErrorState error={audit.error} /> : (audit.data ?? []).length === 0 ? <Empty>No audit entries.</Empty> :
          <Card><CardContent className="divide-y p-0">{(audit.data ?? []).map((a, i) => (<div key={`${a.at}-${i}`} className="p-3 text-sm"><span className="font-mono text-xs">{a.action}</span> <span className="text-xs text-muted-foreground">{a.actor} · {new Date(a.at).toLocaleString()}</span></div>))}</CardContent></Card>)}
    </div>
  );
}
