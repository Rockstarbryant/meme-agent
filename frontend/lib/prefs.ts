"use client";
import { useCallback, useMemo, useSyncExternalStore } from "react";

/** Which in-app toasts this browser shows. Stored in this browser only (localStorage); nothing is sent to the server. */
export interface NotificationPrefs {
  agentState: boolean;      // started / paused / stopped / runner offline
  orders: boolean;          // order placed, position opened, order failed
  exits: boolean;           // take-profit, stop-loss, trailing stop, position closed
  safety: boolean;          // emergency stop, agent errors
}
export const DEFAULT_PREFS: NotificationPrefs = { agentState: true, orders: true, exits: true, safety: true };
const KEY = "arc-agent:notification-prefs:v1";

export type ToastCategory = keyof NotificationPrefs;
/** Which preference controls which live event. Unknown events are not toasted. */
export const EVENT_CATEGORY: Record<string, ToastCategory> = {
  ORDER_SUBMITTED: "orders", POSITION_OPENED: "orders", ORDER_FAILED: "orders",
  TAKE_PROFIT_TRIGGERED: "exits", STOP_LOSS_TRIGGERED: "exits", TRAILING_STOP_TRIGGERED: "exits", POSITION_CLOSED: "exits",
  EMERGENCY_STOP_CHANGED: "safety", AGENT_ERROR: "safety",
};

export function parsePrefs(raw: string | null): NotificationPrefs {
  if (!raw) return DEFAULT_PREFS;
  try {
    const o = JSON.parse(raw) as Partial<Record<keyof NotificationPrefs, unknown>>;
    return {
      agentState: typeof o.agentState === "boolean" ? o.agentState : true, orders: typeof o.orders === "boolean" ? o.orders : true,
      exits: typeof o.exits === "boolean" ? o.exits : true, safety: typeof o.safety === "boolean" ? o.safety : true,
    };
  } catch { return DEFAULT_PREFS; }
}

export function shouldToast(eventType: string, prefs: NotificationPrefs): boolean {
  const cat = EVENT_CATEGORY[eventType];
  return cat ? prefs[cat] : false;
}

export function readPrefs(): NotificationPrefs {
  try { return parsePrefs(typeof window === "undefined" ? null : window.localStorage.getItem(KEY)); } catch { return DEFAULT_PREFS; }
}

function subscribe(cb: () => void): () => void {
  window.addEventListener("storage", cb);
  window.addEventListener("arc-agent:prefs", cb);
  return () => { window.removeEventListener("storage", cb); window.removeEventListener("arc-agent:prefs", cb); };
}
const snapshot = (): string | null => { try { return window.localStorage.getItem(KEY); } catch { return null; } };

export function useNotificationPrefs(): [NotificationPrefs, (patch: Partial<NotificationPrefs>) => void] {
  // Reads localStorage as an external store, so other tabs and the toast watcher stay in sync without effects.
  const raw = useSyncExternalStore(subscribe, snapshot, () => null);
  const prefs = useMemo(() => parsePrefs(raw), [raw]);
  const update = useCallback((patch: Partial<NotificationPrefs>) => {
    const next = { ...readPrefs(), ...patch };
    try { window.localStorage.setItem(KEY, JSON.stringify(next)); } catch { /* storage blocked: the change lasts until reload */ }
    window.dispatchEvent(new Event("arc-agent:prefs"));
  }, []);
  return [prefs, update];
}
