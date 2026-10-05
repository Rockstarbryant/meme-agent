"use client";
import { useEffect, useRef } from "react";
import { useToast } from "@/components/toast";
import { useApi } from "@/hooks/use-api";
import { useEvents } from "@/lib/events";
import { usd } from "@/lib/format";
import type { AgentStatus } from "@/types/api";

/** Human wording for ExitReason values. */
export const EXIT_REASON_LABEL: Record<string, string> = {
  HARD_STOP: "Stop-loss hit", TAKE_PROFIT: "Take-profit hit", TRAILING_STOP: "Trailing stop hit", STAGNATION: "Price stagnated",
  MOMENTUM_DETERIORATION: "Momentum faded", LIQUIDITY_DETERIORATION: "Liquidity dropped", RISK_ESCALATION: "Risk escalated",
  MANUAL: "Closed manually", EMERGENCY: "Emergency stop",
};
export const exitReasonLabel = (r: string | null | undefined) => (r ? EXIT_REASON_LABEL[r] ?? r.replaceAll("_", " ").toLowerCase() : "—");

const STATE_TEXT: Record<string, { title: string; kind: "success" | "info" | "warning" }> = {
  RUNNING: { title: "Agent started: it is running", kind: "success" },
  PAUSED: { title: "Agent paused: no new entries", kind: "info" },
  STOPPED: { title: "Agent stopped: no new entries", kind: "info" },
  OFFLINE: { title: "Runner offline: nothing is being monitored", kind: "warning" },
};

/** Turns live events and agent-state changes into toasts. Mounted once in the app shell. */
export function NotificationWatcher() {
  const { subscribe } = useEvents();
  const { toast, autoSuppressed } = useToast();
  const status = useApi<AgentStatus>("/agent", { intervalMs: 8000 });
  const lastState = useRef<string | null>(null);

  // Agent state changes, whichever device or tab caused them. Only settled states notify, so a slow runner
  // confirming a command does not produce a second toast for the same change.
  useEffect(() => {
    const s = status.data?.state;
    if (!s) return;
    const prev = lastState.current;
    lastState.current = s;
    if (prev === null || prev === s) return;
    const t = STATE_TEXT[s];
    if (t && !autoSuppressed()) toast({ id: "agent-state", kind: t.kind, title: t.title });
  }, [status.data?.state, toast, autoSuppressed]);

  useEffect(() => subscribe((ev) => {
    if (autoSuppressed()) return;
    const p = ev.payload ?? {};
    switch (ev.type) {
      case "ORDER_SUBMITTED":
        toast({ id: `order-${ev.correlation_id}`, kind: "info", title: "Order placed", description: "Waiting for the fill.", href: "/positions", linkLabel: "Positions" });
        break;
      case "POSITION_OPENED":
        toast({ id: `order-${ev.correlation_id}`, kind: "success", title: "Position opened", description: p.simulated ? "Paper trade filled." : "Order filled.", href: "/positions", linkLabel: "View positions" });
        break;
      case "ORDER_FAILED":
        toast({ id: `order-${ev.correlation_id}`, kind: "error", title: "Order failed", description: typeof p.error === "string" ? p.error : "The order could not be completed." });
        break;
      case "TAKE_PROFIT_TRIGGERED":
        toast({ kind: "success", title: "Take-profit triggered", description: "Selling part of the position and holding the rest with a trailing stop.", href: "/positions", linkLabel: "Positions" });
        break;
      case "STOP_LOSS_TRIGGERED":
      case "TRAILING_STOP_TRIGGERED":
        toast({ kind: "warning", title: ev.type === "STOP_LOSS_TRIGGERED" ? "Stop-loss triggered" : "Trailing stop triggered", description: "Closing the position.", href: "/positions", linkLabel: "Positions" });
        break;
      case "POSITION_CLOSED": {
        const pnl = typeof p.realized_pnl === "number" ? p.realized_pnl : null;
        toast({ id: `closed-${ev.correlation_id}`, kind: pnl != null && pnl < 0 ? "warning" : "success", title: "Position closed",
          description: `${exitReasonLabel(typeof p.reason === "string" ? p.reason : null)}${pnl != null ? ` · realized ${pnl >= 0 ? "+" : ""}${usd(pnl)}` : ""}`, href: "/positions", linkLabel: "Positions" });
        break;
      }
      case "EMERGENCY_STOP_CHANGED":
        toast({ kind: p.enabled ? "warning" : "info", title: p.enabled ? "Emergency stop ON: no new entries" : "Emergency stop released" });
        break;
      case "AGENT_ERROR":
        toast({ kind: "error", title: "Agent error", description: typeof p.error === "string" ? p.error.slice(0, 140) : undefined });
        break;
      default:
    }
  }), [subscribe, toast, autoSuppressed]);
  return null;
}
