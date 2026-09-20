"use client";
import { AlertTriangle, Radio } from "lucide-react";
import { ModeBadge } from "@/components/badges";
import { Badge } from "@/components/ui/badge";
import { useApi } from "@/hooks/use-api";
import { useEvents } from "@/lib/events";
import type { AgentStatus } from "@/types/api";

export function ModeBanner({ status, connected }: { status: AgentStatus; connected: boolean }) {
  const demo = status.data_source.toUpperCase().includes("DEMO");
  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <ModeBadge mode={status.mode} />
        {demo && <Badge variant="solidWarning">DEMO DATA</Badge>}
        <Badge variant={status.state === "RUNNING" ? "success" : status.state === "OFFLINE" || status.state === "LIVE_BLOCKED" ? "warning" : "default"}>AGENT {status.state.replace("_", " ")}</Badge>
        <Badge variant={status.runner?.online ? "success" : "warning"}>{status.runner === null ? "NO RUNNER" : status.runner.online ? "RUNNER ONLINE" : "RUNNER OFFLINE"}</Badge>
        <span title={connected ? "Live updates connected" : "Live updates offline"} className="inline-flex items-center gap-1 text-xs text-muted-foreground">
          <Radio className={`h-3 w-3 ${connected ? "text-success" : ""}`} aria-hidden />{connected ? "live" : "offline"}
        </span>
      </div>
      {status.emergency_stop && (
        <div role="alert" className="mt-2 flex items-start gap-2 rounded-md bg-destructive p-2 text-sm font-semibold text-destructive-foreground">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
          <span>EMERGENCY STOP ACTIVE: no new positions. Existing positions stay protected until you disable it.</span>
        </div>
      )}
    </div>
  );
}

export function StatusBanner() {
  const { data } = useApi<AgentStatus>("/agent", { refreshOn: ["EMERGENCY_STOP_CHANGED", "POSITION_OPENED", "POSITION_CLOSED"], intervalMs: 15000 });
  const { connected } = useEvents();
  return data ? <ModeBanner status={data} connected={connected} /> : null;
}
