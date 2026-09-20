import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { Opportunities } from "@/components/features/opportunities";
import { Positions } from "@/components/features/positions";
import { opp, position } from "./fixtures";
import { mockFetch, renderApp } from "./helpers";

const list = [
  opp(),
  opp({ decision_id: "d2", token_key: "arc:0xde04", symbol: "DEMO-RUG", final_action: "REJECT", final_reason: "RISK_VETO: TOP10_CONCENTRATION", top10_holder_pct: 88, strategy_score: 74.2, risk_score: 40 }),
  opp({ decision_id: "d3", token_key: "arc:0xde03", symbol: "DEMO-CHOP", final_action: "WATCH", final_reason: "gate failed: min_unique_buyers_5m", creator_known: false, unique_buyers_5m: 6 }),
];

describe("opportunities", () => {
  it("shows every required field, the decision, and the data label", async () => {
    mockFetch({ "GET /opportunities": list });
    renderApp(<Opportunities />);
    expect(await screen.findByText("DEMO-STRONG")).toBeInTheDocument();
    expect(screen.getAllByText("DEMO DATA")).toHaveLength(3);          // demo data is never presented as live
    expect(screen.queryByText("LIVE DATA")).not.toBeInTheDocument();
    for (const label of ["Age", "Price", "Market cap", "Liquidity", "Volume 5m", "Buyers 5m", "Buy/sell", "Holder growth", "Top-10 holders", "Creator", "Strategy score", "Risk score", "AI"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(screen.getAllByText("BUY")).toHaveLength(2);   // the filter button and the decision badge
    expect(screen.getByText("RISK_VETO: TOP10_CONCENTRATION")).toBeInTheDocument();   // why it was rejected
    expect(screen.getByText("unverified")).toBeInTheDocument();                         // unknown creator is not presented as fine
    expect(screen.getAllByRole("link", { name: "why?" })[0]).toHaveAttribute("href", "/decisions/d1");
    expect(screen.getByRole("link", { name: "DEMO-STRONG" })).toHaveAttribute("href", "/tokens/arc%3A0xde01");
  });

  it("filters by decision", async () => {
    mockFetch({ "GET /opportunities": list });
    renderApp(<Opportunities />);
    await screen.findByText("DEMO-STRONG");
    await userEvent.setup().click(screen.getByRole("button", { name: "REJECT" }));
    expect(screen.queryByText("DEMO-STRONG")).not.toBeInTheDocument();
    expect(screen.getByText("DEMO-RUG")).toBeInTheDocument();
  });

  it("has an empty state and a recoverable error state", async () => {
    let n = 0;
    mockFetch({ "GET /opportunities": () => (n++ === 0 ? { status: 500, body: { detail: "internal error" } } : { body: [] }) });
    renderApp(<Opportunities />);
    expect(await screen.findByText("The server had a problem. Please try again.")).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText(/No opportunities match/)).toBeInTheDocument();
  });
});

describe("positions", () => {
  const rows = [position(), position({ id: "p2", symbol: "DEMO-CRASH", status: "CLOSED", unrealized_pnl_usdc: 0, realized_pnl_usdc: -2.25, gain_pct: -9, exit_reason: "MOMENTUM_DETERIORATION", tiers_hit: [], closed_at: "2026-09-19T12:05:00Z" })];

  it("shows open positions with PAPER label, coloured P&L and take-profit progress", async () => {
    mockFetch({ "GET /positions": rows });
    renderApp(<Positions />);
    expect(await screen.findByText("DEMO-STRONG")).toBeInTheDocument();
    expect(screen.getByText("PAPER")).toBeInTheDocument();
    expect(screen.getByText("+$6.20")).toHaveClass("text-success");
    expect(screen.getByText("+30.0%")).toHaveClass("text-success");
    expect(screen.getByText("1")).toBeInTheDocument();     // take-profit tier 1 hit
    expect(screen.queryByText("DEMO-CRASH")).not.toBeInTheDocument();
  });

  it("shows closed positions with the reason they were closed and losses in red", async () => {
    mockFetch({ "GET /positions": rows });
    renderApp(<Positions />);
    await screen.findByText("DEMO-STRONG");
    await userEvent.setup().click(screen.getByRole("button", { name: "Closed" }));
    expect(screen.getByText("MOMENTUM_DETERIORATION")).toBeInTheDocument();
    expect(screen.getByText("-$2.25")).toHaveClass("text-destructive");
  });

  it("closing a position needs confirmation and then calls the API", async () => {
    const m = mockFetch({ "GET /positions": rows, "POST /agent/close/p1": { queued: true, command_id: "c1" } });
    renderApp(<Positions />);
    const u = userEvent.setup();
    await u.click(await screen.findByRole("button", { name: "Close position" }));
    expect(m.find("POST", "/agent/close/p1")).toHaveLength(0);
    await u.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Close position" }));
    await waitFor(() => expect(m.find("POST", "/agent/close/p1")).toHaveLength(1));
    expect(await screen.findByText(/Close requested\. Your Local Runner will execute it within seconds/)).toBeInTheDocument();
  });

  it("has an empty state", async () => {
    mockFetch({ "GET /positions": [] });
    renderApp(<Positions />);
    expect(await screen.findByText("No open positions.")).toBeInTheDocument();
  });
});
