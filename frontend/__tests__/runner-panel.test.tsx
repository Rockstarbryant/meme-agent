import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { RunnerPanel } from "@/components/features/runner-panel";
import { runnerInfo } from "./fixtures";
import { mockFetch, renderApp } from "./helpers";

describe("Local Runner panel", () => {
  it("explains the architecture: trading on your machine, no keys or wallet session on the server", async () => {
    mockFetch({ "GET /runners": [] });
    renderApp(<RunnerPanel />);
    expect(await screen.findByText(/Trading runs on/)).toHaveTextContent("your own machine");
    expect(screen.getByText(/never holds your funds, keys or wallet session/)).toBeInTheDocument();
    expect(screen.getByText(/nothing connects into your machine/)).toBeInTheDocument();
    expect(screen.getByText(/No runner is paired, so the agent cannot trade/)).toBeInTheDocument();
  });

  it("creates a single-use pairing code and shows the exact command, stating the token carries no wallet authority", async () => {
    const m = mockFetch({ "GET /runners": [], "POST /runners/pairing-codes": { code: "ABCD-EFGH-JKLM", expires_in: 600, command: "x" } });
    renderApp(<RunnerPanel />);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Add runner" }));
    expect(await screen.findByText("ABCD-EFGH-JKLM")).toBeInTheDocument();
    expect(screen.getByText(/python -m runner pair --server http:\/\/localhost:8000 --code ABCD-EFGH-JKLM/)).toBeInTheDocument();
    expect(screen.getByText(/python -m runner run/)).toBeInTheDocument();
    expect(screen.getByText(/carries no wallet or signing authority/)).toBeInTheDocument();
    expect(screen.getByText(/expires in 10 minutes/)).toBeInTheDocument();
    expect(m.find("POST", "/runners/pairing-codes")).toHaveLength(1);
  });

  it("shows why pairing was refused", async () => {
    mockFetch({ "GET /runners": [], "POST /runners/pairing-codes": () => ({ status: 409, body: { detail: "You already have a paired runner. Revoke it first (one active runner per account)." } }) });
    renderApp(<RunnerPanel />);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Add runner" }));
    expect(await screen.findByText(/You already have a paired runner/)).toBeInTheDocument();
  });

  it("shows an online runner with config sync, wallet provider, LIVE reasons and its local ceilings", async () => {
    mockFetch({ "GET /runners": [runnerInfo({ state: "RUNNING", applied_config_version: 4, desired_config_version: 5, wallet_provider: "circle_agent_wallet" })] });
    renderApp(<RunnerPanel />);
    expect(await screen.findByText("laptop")).toBeInTheDocument();
    expect(screen.getByText("ONLINE")).toBeInTheDocument();
    expect(screen.getByText(/Config applied: v4 of v5 \(waiting for the runner to fetch it\)/)).toBeInTheDocument();
    expect(screen.getByText(/Wallet provider on the runner: circle_agent_wallet/)).toBeInTheDocument();
    expect(screen.getByText(/LIVE is disabled locally on this runner/)).toBeInTheDocument();
    expect(screen.getByText(/the server cannot exceed them/)).toBeInTheDocument();
    expect(screen.getByText("max trade")).toBeInTheDocument();
  });

  it("warns when the runner is offline or has suspended entries (dead-man switch) or reports an error", async () => {
    mockFetch({ "GET /runners": [runnerInfo({ online: false, entries_suspended_reason: "control plane unreachable for more than 60s: no NEW entries until it is back", last_error: "heartbeat: unreachable" })] });
    renderApp(<RunnerPanel />);
    expect(await screen.findByText("OFFLINE")).toBeInTheDocument();
    expect(screen.getByText(/Your runner is not connected/)).toBeInTheDocument();
    expect(screen.getByText(/New entries are suspended: control plane unreachable/)).toBeInTheDocument();
    expect(screen.getByText(/Last error: heartbeat: unreachable/)).toBeInTheDocument();
  });

  it("revoking asks for confirmation and warns positions will no longer be managed", async () => {
    const m = mockFetch({ "GET /runners": [runnerInfo()], "DELETE /runners/r1": { revoked: true } });
    renderApp(<RunnerPanel />);
    const u = userEvent.setup();
    await u.click(await screen.findByRole("button", { name: "Revoke runner" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("no longer managed by anything");
    expect(m.find("DELETE", "/runners/r1")).toHaveLength(0);
    await u.click(within(dialog).getByRole("button", { name: "Revoke runner" }));
    await waitFor(() => expect(m.find("DELETE", "/runners/r1")).toHaveLength(1));
  });

  it("ignores revoked runners when deciding whether one is paired", async () => {
    mockFetch({ "GET /runners": [runnerInfo({ revoked_at: "2026-09-19T01:00:00Z" })] });
    renderApp(<RunnerPanel />);
    expect(await screen.findByText(/No runner is paired/)).toBeInTheDocument();
  });
});
