import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { Activity } from "@/components/features/activity";
import { SettingsView } from "@/components/features/settings";
import { Strategies } from "@/components/features/strategies";
import { agent, chain, limits } from "./fixtures";
import { mockFetch, renderApp, type Call } from "./helpers";

const cfg = { strategy_id: "traction_momentum", version: 1, min_score: 70, watch_score: 50, weights: { momentum: 0.25, buyer_growth: 0.2, buy_sell_pressure: 0.15, liquidity_quality: 0.15, holder_distribution: 0.1, creator_behavior: 0.1, market_quality: 0.05 },
  exit: { hard_stop_pct: 20, trailing_stop_pct: 20, tiers: [{ gain_pct: 30, sell_pct_of_initial: 15 }] } };

describe("strategies", () => {
  const routes = () => ({ "GET /strategies": [{ id: "traction_momentum", name: "Traction Momentum", description: "Buys only tokens showing traction.", enabled: true, active: true }],
    "GET /strategies/traction_momentum/versions": [{ version: 1, scope: "default", created_at: "2026-09-19T00:00:00Z", config: cfg }] });

  it("shows the active version and creates a new immutable version preserving untouched settings", async () => {
    const m = mockFetch({ ...routes(), "POST /strategies/traction_momentum/versions": { version: 2 } });
    renderApp(<Strategies />);
    const u = userEvent.setup();
    expect(await screen.findByText("v1 active")).toBeInTheDocument();
    const min = await screen.findByLabelText("Qualify score");
    await u.clear(min); await u.type(min, "80");
    await u.click(screen.getByRole("button", { name: "Save as new version" }));
    await waitFor(() => expect(m.find("POST", "/strategies/traction_momentum/versions")).toHaveLength(1));
    const body = m.find("POST", "/strategies/traction_momentum/versions")[0].body as { config: Record<string, unknown>; confirm: boolean };
    expect(body.config.min_score).toBe(80);
    expect(body.config.strategy_id).toBeUndefined();
    expect(body.config.version).toBeUndefined();
    expect((body.config.exit as { tiers: unknown[] }).tiers).toHaveLength(1);   // exit ladder preserved
    expect(body.confirm).toBe(false);
    expect(await screen.findByText("Saved as version 2.")).toBeInTheDocument();
  });

  it("disables and re-enables the strategy for your runner", async () => {
    let enabled = true;
    const m = mockFetch({ ...routes(), "GET /strategies": () => ({ body: [{ id: "traction_momentum", name: "Traction Momentum", description: "d", enabled, active: true }] }),
      "PUT /strategies/traction_momentum/enabled": (c: Call) => { enabled = (c.body as { enabled: boolean }).enabled; return { body: { strategy: "traction_momentum", enabled } }; } });
    renderApp(<Strategies />);
    const u = userEvent.setup();
    expect(await screen.findByText("ENABLED")).toBeInTheDocument();
    await u.click(screen.getByRole("button", { name: "Disable strategy" }));
    await waitFor(() => expect(m.find("PUT", "/strategies/traction_momentum/enabled")[0].body).toEqual({ enabled: false }));
    expect(await screen.findByText("DISABLED")).toBeInTheDocument();
    expect(screen.getByText(/keeps managing open ones/)).toBeInTheDocument();
  });

  it("blocks a watch score above the qualify score", async () => {
    mockFetch(routes());
    renderApp(<Strategies />);
    const watch = await screen.findByLabelText("Watch score");
    await userEvent.setup().type(watch, "5");
    expect(await screen.findByText(/Watch score cannot exceed/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save as new version" })).toBeDisabled();
  });
});

describe("settings", () => {
  it("lists disabled/unverified launchpads, hides AI keys, marks notifications unimplemented, and has the kill switch", async () => {
    mockFetch({ "GET /settings": { mode: "PAPER", live_trading_enabled_on_server: false, ai: { provider: null, model: null, note: "Provider and model are set by server environment; keys are never exposed." },
        market_data: "DEMO DATA (synthetic, PAPER only)", notifications: { implemented: false } },
      "GET /agent": agent(), "GET /chains": [chain()], "GET /launchpads": { launchpads: [{ id: "l1", chain: "arc", name: "pools", verified: false, enabled: false, descriptor: {} }], note: "No launchpad is enabled until verified." },
      "GET /risk/limits": { limits: limits(), effective_limits: limits() }, "GET /controls/blacklist": { tokens: ["0xbad"], creators: [], launchpads: [] } });
    renderApp(<SettingsView />);
    expect(await screen.findByText("pools")).toBeInTheDocument();
    expect(screen.getByText("unverified")).toBeInTheDocument();
    expect(screen.getByText("disabled")).toBeInTheDocument();
    expect(screen.getByText(/keys are never exposed/)).toBeInTheDocument();
    expect(screen.getByText("Not implemented yet.")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "EMERGENCY STOP" })).toBeInTheDocument();
    expect(await screen.findByText(/0xbad/)).toBeInTheDocument();
    expect(screen.getByText(/BNB Chain, Solana and Robinhood Chain are extension points, not integrations/)).toBeInTheDocument();
  });
});

describe("activity", () => {
  it("merges live stream events with stored history without duplicates", async () => {
    const stored = [{ id: "e1", type: "ORDER_FILLED", at: "2026-09-19T12:00:00Z", correlation_id: "d1", payload: { status: "FILLED" } }];
    mockFetch({ "GET /activity": stored });
    renderApp(<Activity />, { events: [{ id: "e2", type: "POSITION_CLOSED", at: "2026-09-19T12:01:00Z", correlation_id: "p1", payload: { reason: "TRAILING_STOP" } }, { id: "e1", type: "ORDER_FILLED", at: "2026-09-19T12:00:00Z", correlation_id: "d1", payload: {} }] });
    expect(await screen.findByText("POSITION_CLOSED")).toBeInTheDocument();
    expect(screen.getAllByText("ORDER_FILLED", { selector: "span" })).toHaveLength(1);   // deduplicated (the <option> is the filter)
  });

  it("shows the audit log tab", async () => {
    mockFetch({ "GET /activity": [], "GET /audit-logs": [{ at: "2026-09-19T12:00:00Z", actor: "test@example.com", action: "EMERGENCY_STOP", detail: {} }] });
    renderApp(<Activity />);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Audit log" }));
    expect(await screen.findByText("EMERGENCY_STOP")).toBeInTheDocument();
  });
});
