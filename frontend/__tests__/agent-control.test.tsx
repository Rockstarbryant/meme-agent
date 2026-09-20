import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { AgentControl } from "@/components/features/agent-control";
import { agent, runnerInfo, wallet } from "./fixtures";
import { mockFetch, renderApp } from "./helpers";

const base = (over: Record<string, unknown> = {}) => ({ "GET /agent": agent(), "GET /wallet": wallet(), "GET /runners": [runnerInfo()], ...over });

describe("agent controls", () => {
  it("shows PAPER mode, wallet capability and effective limits", async () => {
    mockFetch(base());
    renderApp(<AgentControl />);
    expect(await screen.findByText("PAPER MODE")).toBeInTheDocument();
    expect(await screen.findByText("Paper trading only")).toBeInTheDocument();
    expect(screen.getByText(/Effective risk limits/)).toHaveTextContent("max trade $25.00");
    expect(screen.getByText("traction_momentum v1")).toBeInTheDocument();
  });

  it("START posts to the API and refreshes", async () => {
    const m = mockFetch(base({ "POST /agent/start": agent({ state: "RUNNING", desired_state: "RUNNING" }) }));
    renderApp(<AgentControl />);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Start" }));
    await waitFor(() => expect(m.find("POST", "/agent/start")).toHaveLength(1));
  });

  it("disables START while an emergency stop is active", async () => {
    mockFetch(base({ "GET /agent": agent({ emergency_stop: true }) }));
    renderApp(<AgentControl />);
    expect(await screen.findByRole("button", { name: "Start" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Disable emergency stop" })).toBeInTheDocument();
  });

  it("emergency stop needs an explicit confirmation before it is sent", async () => {
    const m = mockFetch(base({ "POST /agent/emergency-stop": agent({ emergency_stop: true }) }));
    renderApp(<AgentControl />);
    const u = userEvent.setup();
    await u.click(await screen.findByRole("button", { name: "EMERGENCY STOP" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Existing positions keep their stop-loss");
    expect(m.find("POST", "/agent/emergency-stop")).toHaveLength(0);
    await u.click(within(dialog).getByRole("button", { name: "Enable emergency stop" }));
    await waitFor(() => expect(m.find("POST", "/agent/emergency-stop")[0].body).toEqual({ enabled: true }));
  });

  it("says why LIVE is blocked and keeps the confirm button locked until the exact phrase is typed", async () => {
    const m = mockFetch(base());
    renderApp(<AgentControl />);
    const u = userEvent.setup();
    expect(await screen.findByText("Currently blocked because:")).toBeInTheDocument();
    expect(screen.getByText(/Arc live integration .* not verified/)).toBeInTheDocument();
    await u.click(screen.getByRole("button", { name: /Switch to LIVE/ }));
    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Enable LIVE" });
    expect(confirm).toBeDisabled();
    await u.type(within(dialog).getByLabelText("confirmation phrase"), "enable live trading");
    expect(confirm).toBeDisabled();
    await u.clear(within(dialog).getByLabelText("confirmation phrase"));
    await u.type(within(dialog).getByLabelText("confirmation phrase"), "ENABLE LIVE TRADING");
    expect(confirm).toBeEnabled();
    expect(m.find("POST", "/agent/mode")).toHaveLength(0);
  });

  it("shows the server's blockers when LIVE is refused, and stays in PAPER", async () => {
    const m = mockFetch(base({ "POST /agent/mode": () => ({ status: 409, body: { detail: { error: "LIVE_NOT_READY", blockers: ["Arc live integration is not verified", "No wallet with verified ownership"] } } }) }));
    renderApp(<AgentControl />);
    const u = userEvent.setup();
    await u.click(await screen.findByRole("button", { name: /Switch to LIVE/ }));
    const dialog = await screen.findByRole("dialog");
    await u.type(within(dialog).getByLabelText("confirmation phrase"), "ENABLE LIVE TRADING");
    await u.click(within(dialog).getByRole("button", { name: "Enable LIVE" }));
    await waitFor(() => expect(m.find("POST", "/agent/mode")[0].body).toEqual({ mode: "LIVE", confirmation: "ENABLE LIVE TRADING" }));
    expect(await within(await screen.findByRole("dialog")).findByText("No wallet with verified ownership")).toBeInTheDocument();
    expect(screen.getAllByText("PAPER MODE").length).toBeGreaterThan(0);
  });

  it("in LIVE mode warns loudly and offers a way back to PAPER", async () => {
    mockFetch(base({ "GET /agent": agent({ mode: "LIVE", mode_label: "LIVE MODE", live_blockers: [] }) }));
    renderApp(<AgentControl />);
    expect(await screen.findByText("LIVE MODE")).toBeInTheDocument();
    expect(screen.getByText(/Trades use real funds/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Switch back to PAPER" })).toBeInTheDocument();
  });

  it("reports API failures instead of failing silently", async () => {
    mockFetch(base({ "POST /agent/start": () => ({ status: 500, body: { detail: "internal error" } }) }));
    renderApp(<AgentControl />);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Start" }));
    expect(await screen.findByText("The server had a problem. Please try again.")).toBeInTheDocument();
  });

  it("separates what you asked for from what your runner reports", async () => {
    mockFetch(base({ "GET /agent": agent({ desired_state: "RUNNING", state: "PAUSED", config_version: 5, applied_config_version: 4 }) }));
    renderApp(<AgentControl />);
    const line = await screen.findByText(/Requested:/);
    expect(line).toHaveTextContent("Requested: RUNNING");
    expect(line).toHaveTextContent("Reported by your runner: PAUSED");
    expect(line).toHaveTextContent("waiting for the runner to apply your latest change");
    expect(screen.getByRole("button", { name: "Pause" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();       // already requested
  });

  it("tells you plainly when the runner is offline and nothing is being monitored", async () => {
    mockFetch(base({ "GET /agent": agent({ state: "OFFLINE", runner: { ...runnerInfo(), online: false } }) }));
    renderApp(<AgentControl />);
    expect(await screen.findByText(/Your Local Runner is offline, so nothing is trading or being monitored/)).toBeInTheDocument();
  });

  it("explains that LIVE was refused by the runner and never fell back to PAPER", async () => {
    mockFetch(base({ "GET /agent": agent({ mode: "LIVE", mode_label: "LIVE MODE", state: "LIVE_BLOCKED", desired_state: "RUNNING", live_blockers: [] }) }));
    renderApp(<AgentControl />);
    expect(await screen.findByText(/refused to trade LIVE \(it never falls back to PAPER\)/)).toBeInTheDocument();
  });

  it("start without a paired runner shows the server's instruction", async () => {
    mockFetch(base({ "GET /agent": agent({ runner: null, state: "OFFLINE" }), "GET /runners": [], "POST /agent/start": () => ({ status: 409, body: { detail: "Pair a Local Runner first: trading runs on your own machine, not on this server." } }) }));
    renderApp(<AgentControl />);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Start" }));
    expect(await screen.findByText(/Pair a Local Runner first/)).toBeInTheDocument();
  });
});
