import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useApi } from "@/hooks/use-api";
import { AuthContext, type AuthCtx } from "@/lib/auth";
import { EventsProvider, useEvents } from "@/lib/events";
import { mockFetch, TEST_USER } from "./helpers";

class FakeES {
  static all: FakeES[] = [];
  url: string; closed = false; onerror: (() => void) | null = null; private l: Record<string, ((e: MessageEvent) => void)[]> = {};
  constructor(url: string) { this.url = url; FakeES.all.push(this); }
  addEventListener(t: string, fn: (e: MessageEvent) => void) { (this.l[t] ??= []).push(fn); }
  close() { this.closed = true; }
  emit(t: string, data: unknown) { (this.l[t] ?? []).forEach((f) => f({ data: JSON.stringify(data) } as MessageEvent)); }
}
const auth: AuthCtx = { user: TEST_USER, ready: true, login: vi.fn(), register: vi.fn(), logout: vi.fn(), refresh: vi.fn() };

function Probe() {
  const { connected } = useEvents();
  const pf = useApi<{ cash: number }>("/portfolio", { refreshOn: ["ORDER_FILLED"] });
  return <div><span>{connected ? "live" : "offline"}</span><span data-testid="cash">{pf.data?.cash ?? "…"}</span></div>;
}
const wrap = () => render(<AuthContext.Provider value={auth}><EventsProvider><Probe /></EventsProvider></AuthContext.Provider>);

beforeEach(() => { FakeES.all = []; vi.stubGlobal("EventSource", FakeES); });
afterEach(() => vi.unstubAllGlobals());

describe("live updates (SSE)", () => {
  it("trades the bearer token for a single-use ticket and never puts the JWT in the URL", async () => {
    mockFetch({ "POST /stream/ticket": { ticket: "tk-1", expires_in: 60 }, "GET /portfolio": { cash: 1000 } });
    wrap();
    await waitFor(() => expect(FakeES.all).toHaveLength(1));
    expect(FakeES.all[0].url).toMatch(/\/stream\?ticket=tk-1$/);
    expect(FakeES.all[0].url).not.toMatch(/token|jwt|bearer/i);
  });

  it("shows connected after hello, refetches on a matching event, and ignores unrelated ones", async () => {
    let cash = 1000;
    const m = mockFetch({ "POST /stream/ticket": { ticket: "tk-1" }, "GET /portfolio": () => ({ body: { cash } }) });
    wrap();
    await waitFor(() => expect(FakeES.all).toHaveLength(1));
    expect(screen.getByText("offline")).toBeInTheDocument();
    act(() => FakeES.all[0].emit("hello", { user_id: "u1" }));
    expect(screen.getByText("live")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("cash")).toHaveTextContent("1000"));
    const before = m.find("GET", "/portfolio").length;

    act(() => FakeES.all[0].emit("RISK_ALERT", { id: "e0", type: "RISK_ALERT", at: "x", correlation_id: "", payload: {} }));
    await new Promise((r) => setTimeout(r, 600));
    expect(m.find("GET", "/portfolio")).toHaveLength(before);              // unrelated event: no refetch

    cash = 975;
    act(() => FakeES.all[0].emit("ORDER_FILLED", { id: "e1", type: "ORDER_FILLED", at: "x", correlation_id: "d1", payload: {} }));
    await waitFor(() => expect(screen.getByTestId("cash")).toHaveTextContent("975"), { timeout: 2000 });
  });

  it("closes the stream on error and on unmount", async () => {
    mockFetch({ "POST /stream/ticket": { ticket: "tk-1" }, "GET /portfolio": { cash: 1 } });
    const { unmount } = wrap();
    await waitFor(() => expect(FakeES.all).toHaveLength(1));
    act(() => FakeES.all[0].emit("hello", {}));
    act(() => FakeES.all[0].onerror?.());
    expect(FakeES.all[0].closed).toBe(true);
    expect(screen.getByText("offline")).toBeInTheDocument();
    unmount();
  });
});
