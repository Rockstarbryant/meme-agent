import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "@/components/app-shell";
import { Dashboard } from "@/components/features/dashboard";
import { agent, chain, opp, order, portfolio, wallet } from "./fixtures";
import { mockFetch, renderApp } from "./helpers";

const nav = vi.hoisted(() => ({ replace: vi.fn(), path: "/" }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: nav.replace }), usePathname: () => nav.path, useParams: () => ({}) }));
beforeEach(() => { nav.replace.mockClear(); nav.path = "/"; });

const dash = () => mockFetch({ "GET /portfolio": portfolio(), "GET /agent": agent(), "GET /opportunities": [opp()], "GET /orders": [order()], "GET /chains": [chain()], "GET /wallet": wallet() });

describe("dashboard", () => {
  it("shows portfolio, P&L, limits, agent, wallet, network, decisions and trades", async () => {
    dash();
    renderApp(<Dashboard />);
    expect(await screen.findByText("Portfolio value (PAPER)")).toBeInTheDocument();
    expect(screen.getByText("$1,006.20")).toBeInTheDocument();
    expect(screen.getByText("$975.00")).toBeInTheDocument();
    expect(screen.getByText("1 / 5")).toBeInTheDocument();
    expect(await screen.findByText("Daily loss $10.00 of $50.00 used")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "20");
    expect(await screen.findByText("DEMO-STRONG")).toBeInTheDocument();
    expect(await screen.findByText("PAPER (SIMULATED)")).toBeInTheDocument();
    expect(await screen.findByText("online (block 123456)")).toBeInTheDocument();
    expect(screen.getByText("Paper trading only")).toBeInTheDocument();
    expect(await screen.findByText(/LIVE trading integration not verified: PAPER only/)).toBeInTheDocument();
  });

  it("shows an error with retry when the API is down", async () => {
    mockFetch({ "GET /portfolio": () => ({ status: 500, body: { detail: "internal error" } }) });
    renderApp(<Dashboard />);
    expect(await screen.findByText("The server had a problem. Please try again.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});

describe("app shell", () => {
  it("redirects unauthenticated visitors to /login", async () => {
    mockFetch({});
    renderApp(<AppShell><p>secret</p></AppShell>, { user: null });
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/login"));
    expect(screen.queryByText("secret")).not.toBeInTheDocument();
  });

  it("renders every page in desktop and mobile navigation and marks the current page", async () => {
    nav.path = "/wallet";
    mockFetch({ "GET /agent": agent({ emergency_stop: true }) });
    renderApp(<AppShell><p>content</p></AppShell>);
    expect(await screen.findByText("content")).toBeInTheDocument();
    for (const name of ["Dashboard", "Opportunities", "Positions", "Agent", "Strategies", "Activity", "Wallet", "Settings"]) {
      expect(screen.getAllByRole("link", { name })).toHaveLength(2);        // sidebar (md+) and bottom bar (mobile)
    }
    expect(screen.getAllByRole("link", { name: "Wallet" })[0]).toHaveAttribute("aria-current", "page");
    expect(await screen.findByText(/EMERGENCY STOP ACTIVE/)).toBeInTheDocument();  // visible on every page
    expect(screen.getByText("PAPER MODE")).toBeInTheDocument();
  });
});
