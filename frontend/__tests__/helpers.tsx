import type { ReactElement } from "react";
import { render } from "@testing-library/react";
import { vi } from "vitest";
import { AuthContext, type AuthCtx } from "@/lib/auth";
import { EventsContext, type EventsCtx } from "@/lib/events";
import type { StreamEvent, User } from "@/types/api";

export interface Call { method: string; path: string; body: unknown }
type Reply = { status?: number; body: unknown };
export type Handler = unknown | ((c: Call) => Reply);

/** Route-table fetch mock: keys are "METHOD /path" (query string optional). Unmocked routes 404 loudly. */
export function mockFetch(routes: Record<string, Handler>) {
  const calls: Call[] = [];
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    const path = String(url).replace(/^https?:\/\/[^/]+/, "");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const call = { method, path, body };
    calls.push(call);
    const h = routes[`${method} ${path}`] ?? routes[`${method} ${path.split("?")[0]}`];
    if (h === undefined) return new Response(JSON.stringify({ detail: `no mock for ${method} ${path}` }), { status: 404 });
    const r: Reply = typeof h === "function" ? (h as (c: Call) => Reply)(call) : { body: h };
    return new Response(JSON.stringify(r.body), { status: r.status ?? 200, headers: { "content-type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return { calls, fn, find: (method: string, path: string) => calls.filter((c) => c.method === method && c.path.split("?")[0] === path) };
}

export const TEST_USER: User = { id: "u1", email: "test@example.com", mode: "PAPER", created_at: "2026-09-19T00:00:00Z" };

export function renderApp(ui: ReactElement, opts: { user?: User | null; connected?: boolean; events?: StreamEvent[] } = {}) {
  const listeners = new Set<(e: StreamEvent) => void>();
  const auth: AuthCtx = {
    user: opts.user === undefined ? TEST_USER : opts.user, ready: true,
    login: vi.fn(async () => undefined), register: vi.fn(async () => undefined), logout: vi.fn(async () => undefined), refresh: vi.fn(async () => undefined),
  };
  const ev: EventsCtx = { connected: opts.connected ?? false, events: opts.events ?? [], subscribe: (fn) => { listeners.add(fn); return () => { listeners.delete(fn); }; } };
  const utils = render(<AuthContext.Provider value={auth}><EventsContext.Provider value={ev}>{ui}</EventsContext.Provider></AuthContext.Provider>);
  return { ...utils, auth, emit: (e: StreamEvent) => listeners.forEach((l) => l(e)) };
}
