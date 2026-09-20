"use client";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { API_URL, api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { StreamEvent } from "@/types/api";

const TYPES = ["TOKEN_LAUNCHED", "TRADE_DETECTED", "TOKEN_ACTIVITY_UPDATED", "RISK_ASSESSMENT_CREATED", "STRATEGY_SIGNAL_CREATED",
  "AI_ANALYSIS_COMPLETED", "DECISION_RECORDED", "BUY_APPROVED", "BUY_REJECTED", "ORDER_SUBMITTED", "ORDER_FILLED", "ORDER_FAILED",
  "POSITION_OPENED", "POSITION_UPDATED", "TAKE_PROFIT_TRIGGERED", "STOP_LOSS_TRIGGERED", "TRAILING_STOP_TRIGGERED",
  "POSITION_CLOSED", "EMERGENCY_STOP_CHANGED", "RISK_ALERT", "AGENT_ERROR"];

type Listener = (e: StreamEvent) => void;
export interface EventsCtx { connected: boolean; events: StreamEvent[]; subscribe: (fn: Listener) => () => void }
export const EventsContext = createContext<EventsCtx>({ connected: false, events: [], subscribe: () => () => undefined });
export const useEvents = () => useContext(EventsContext);

/** Live updates via SSE. EventSource cannot send headers, so we trade the bearer token for a 60s single-use ticket. */
export function EventsProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const [connected, setConnected] = useState(false);
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const listeners = useRef(new Set<Listener>());

  useEffect(() => {
    if (!user) return;
    let es: EventSource | null = null;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const retry = (ms: number) => { if (!stopped) timer = setTimeout(() => void connect(), ms); };
    async function connect() {
      try {
        const { ticket } = await api<{ ticket: string }>("/stream/ticket", { method: "POST" });
        if (stopped) return;
        const source = new EventSource(`${API_URL}/stream?ticket=${encodeURIComponent(ticket)}`);
        es = source;
        source.addEventListener("hello", () => setConnected(true));
        for (const t of TYPES) {
          source.addEventListener(t, (m) => {
            const ev = JSON.parse((m as MessageEvent<string>).data) as StreamEvent;
            setEvents((prev) => [ev, ...prev].slice(0, 100));
            listeners.current.forEach((l) => l(ev));
          });
        }
        source.onerror = () => { setConnected(false); source.close(); retry(3000); };
      } catch {
        setConnected(false);
        retry(5000);
      }
    }
    void connect();
    return () => { stopped = true; clearTimeout(timer); es?.close(); setConnected(false); };
  }, [user]);

  // Must be referentially stable: consumers re-subscribe when it changes, which would cancel their pending refetch timers.
  const subscribe = useCallback((fn: Listener) => { listeners.current.add(fn); return () => { listeners.current.delete(fn); }; }, []);
  const value = useMemo<EventsCtx>(() => ({ connected, events, subscribe }), [connected, events, subscribe]);
  return <EventsContext.Provider value={value}>{children}</EventsContext.Provider>;
}
