import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WalletPanel, validatePolicy } from "@/components/features/wallet-panel";
import { wallet } from "./fixtures";
import { mockFetch, renderApp } from "./helpers";

const ADDR = "0x1111111111111111111111111111111111111111";
const routes = (over: Record<string, unknown> = {}) => ({ "GET /wallet": wallet(), "GET /wallet/signing-requests": [], "GET /wallet/activity": [], ...over });
afterEach(() => { delete window.ethereum; });

describe("wallet page", () => {
  it("states all three capabilities explicitly and that autonomous execution is unavailable", async () => {
    mockFetch(routes());
    renderApp(<WalletPanel />);
    expect(await screen.findByText("Paper trading only", { selector: "p" })).toBeInTheDocument();
    expect(screen.getByText(/Autonomous delegated execution:/)).toBeInTheDocument();
    expect(screen.getByText(/Explicit per-trade signing:/).closest("li")).toHaveTextContent("inactive");
    expect(screen.getByText(/Paper trading:/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Authorize autonomous delegation" })).toBeDisabled();
  });

  it("shows the wallet that lives on your Local Runner and why LIVE is unavailable", async () => {
    const live = { available: false, reasons: ["Circle CLI JSON output schemas are unverified", "LIVE is disabled locally on this runner"], provider: "circle_agent_wallet", session_authorized: false, address: null, chain_verified: false, locally_enabled: false };
    mockFetch(routes({ "GET /wallet": wallet({ runner_wallet: { provider: "circle_agent_wallet", online: true, live } }) }));
    renderApp(<WalletPanel />);
    expect(await screen.findByText("Wallet on your Local Runner")).toBeInTheDocument();
    expect(screen.getByText("circle_agent_wallet")).toBeInTheDocument();
    expect(screen.getByText("Circle CLI JSON output schemas are unverified")).toBeInTheDocument();
    expect(screen.getByText(/Your wallet session and keys stay on your machine/)).toBeInTheDocument();
  });

  it("says there is no autonomous wallet when no runner is paired", async () => {
    mockFetch(routes());
    renderApp(<WalletPanel />);
    expect(await screen.findByText(/No runner is paired, so there is no autonomous wallet/)).toBeInTheDocument();
    expect(screen.getByText("Browser wallet (optional, manual signing)")).toBeInTheDocument();
  });

  it("is honest about the Circle Agent Wallet and marks developer-controlled wallets as forbidden", async () => {
    mockFetch(routes());
    renderApp(<WalletPanel />);
    expect(await screen.findByText(/Circle Agent Wallet.*: not integrated/)).toBeInTheDocument();
    expect(screen.getByText(/docs\/local-runner\.md/)).toBeInTheDocument();
    expect(screen.getByText("Forbidden (custodial)")).toBeInTheDocument();
    expect(screen.getByText("Available")).toBeInTheDocument();
    expect(screen.getByText(/whether contract writes count toward the USDC caps/)).toBeInTheDocument();
  });

  it("shows the single ERC-20 USDC view, capital and network health", async () => {
    mockFetch(routes({ "GET /wallet": wallet({ usdc: { balance: 12.345678, view: "ERC-20, 6 decimals", address: "0x36", note: "ERC-20 (6-decimal) view of the single USDC balance" }, allocated_capital_usdc: 500 }) }));
    renderApp(<WalletPanel />);
    expect(await screen.findByText("$12.35")).toBeInTheDocument();
    expect(screen.getByText("(ERC-20, 6 decimals)")).toBeInTheDocument();
    expect(screen.getByText(/Arc mainnet \(chain id 5042\)/)).toHaveTextContent("unavailable");
    expect(screen.getByText("PAPER (virtual USDC)")).toBeInTheDocument();
  });

  it("reflects an active per-trade-signing authorization", async () => {
    const w = wallet({ wallets: [{ id: "w1", provider: "browser_wallet", address: ADDR, ownership_verified: true }],
      execution_capability: { capability: "PER_TRADE_SIGNING", label: "Explicit per-trade signing", autonomous: false, detail: "Every live trade needs your wallet signature." },
      authorization: { id: "a1", capability: "PER_TRADE_SIGNING", granted_at: "2026-09-19T00:00:00Z", expires_at: null } });
    mockFetch(routes({ "GET /wallet": w }));
    renderApp(<WalletPanel />);
    expect(await screen.findByText("ownership verified")).toBeInTheDocument();
    expect(screen.getByText(/Explicit per-trade signing:/).closest("li")).toHaveTextContent("active");
    expect(screen.getByRole("button", { name: "Revoke authorization" })).toBeEnabled();
  });

  it("connects by signing a challenge: switches to Arc, signs, and never asks for a key", async () => {
    const request = vi.fn(async ({ method }: { method: string }) => (method === "eth_requestAccounts" ? [ADDR] : method === "personal_sign" ? "0xsignature" : null));
    window.ethereum = { request };
    const m = mockFetch(routes({ "POST /wallet/challenge": { message: "Link wallet to Arc Agent. Nonce: abc", expires_in: 300 }, "POST /wallet/connect": { id: "w1", ownership_verified: true } }));
    renderApp(<WalletPanel />);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Connect wallet" }));
    await waitFor(() => expect(m.find("POST", "/wallet/connect")).toHaveLength(1));
    expect(request).toHaveBeenCalledWith({ method: "wallet_switchEthereumChain", params: [{ chainId: "0x13b2" }] });
    expect(m.find("POST", "/wallet/challenge")[0].body).toEqual({ address: ADDR });
    const signCall = request.mock.calls.find(([c]) => c.method === "personal_sign")?.[0] as unknown as { params: string[] };
    expect(signCall.params[1]).toBe(ADDR);
    expect(signCall.params[0].startsWith("0x")).toBe(true);
    expect(m.find("POST", "/wallet/connect")[0].body).toEqual({ address: ADDR, signature: "0xsignature" });
    expect(request.mock.calls.map(([c]) => c.method)).not.toContain("eth_sendTransaction");
    expect(await screen.findByText(/no funds moved/)).toBeInTheDocument();
  });

  it("explains when no browser wallet is installed", async () => {
    mockFetch(routes());
    renderApp(<WalletPanel />);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Connect wallet" }));
    expect(await screen.findByText(/No browser wallet found/)).toBeInTheDocument();
  });

  it("validates the policy and blocks saving an inconsistent one", async () => {
    const m = mockFetch(routes({ "POST /wallet/policy": { version: 1 } }));
    renderApp(<WalletPanel />);
    const u = userEvent.setup();
    const maxTrade = await screen.findByLabelText("Maximum trade (USDC)");
    await u.clear(maxTrade); await u.type(maxTrade, "100");
    expect(screen.getByText("Maximum trade cannot exceed maximum position.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save policy" })).toBeDisabled();
    await u.clear(maxTrade); await u.type(maxTrade, "20");
    await u.click(screen.getByRole("button", { name: "Save policy" }));
    await waitFor(() => expect(m.find("POST", "/wallet/policy")[0].body).toMatchObject({ max_trade_usdc: 20, allocated_capital_usdc: 500 }));
    expect(await screen.findByText(/Existing authorizations were revoked/)).toBeInTheDocument();
  });

  it("revoking asks for confirmation first", async () => {
    const w = wallet({ authorization: { id: "a1", capability: "PER_TRADE_SIGNING", granted_at: "2026-09-19T00:00:00Z", expires_at: null } });
    const m = mockFetch(routes({ "GET /wallet": w, "POST /wallet/revoke": { revoked: 1, switched_to_paper: false } }));
    renderApp(<WalletPanel />);
    const u = userEvent.setup();
    await u.click(await screen.findByRole("button", { name: "Revoke authorization" }));
    expect(m.find("POST", "/wallet/revoke")).toHaveLength(0);
    await u.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(m.find("POST", "/wallet/revoke")).toHaveLength(1));
  });

  it("validatePolicy covers the edge cases", () => {
    const ok = { allocated_capital_usdc: 500, max_trade_usdc: 25, max_position_usdc: 50, max_daily_loss_usdc: 50, max_open_positions: 5, max_slippage_pct: 2, min_liquidity_usdc: 10000 };
    expect(validatePolicy(ok)).toBeNull();
    expect(validatePolicy({ ...ok, max_position_usdc: 600 })).toMatch(/allocated capital/);
    expect(validatePolicy({ ...ok, max_slippage_pct: 25 })).toMatch(/20%/);
    expect(validatePolicy({ ...ok, max_open_positions: 2.5 })).toMatch(/whole number/);
    expect(validatePolicy({ ...ok, max_trade_usdc: NaN })).toMatch(/greater than zero/);
  });
});
