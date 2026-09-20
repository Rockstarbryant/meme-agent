import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DecisionDetail } from "@/components/features/decision-detail";
import { TokenDetail } from "@/components/features/token-detail";
import { ensureArcNetwork, personalSign, sendSigningRequest, WalletError } from "@/lib/wallet-browser";
import { ago, duration, pct, price, usd } from "@/lib/format";
import { decision, tokenDetail, wallet } from "./fixtures";
import { mockFetch, renderApp } from "./helpers";

afterEach(() => { delete window.ethereum; });

describe("decision detail: why did the agent act?", () => {
  it("reconstructs saw -> strategy -> risk -> AI -> execution -> timeline", async () => {
    mockFetch({ "GET /decisions/d1": decision() });
    renderApp(<DecisionDetail decisionId="d1" />);
    expect(await screen.findByText("RISK_VETO: TOP10_CONCENTRATION")).toBeInTheDocument();
    expect(screen.getByText("2. Strategy: traction_momentum v1")).toBeInTheDocument();
    expect(screen.getByText("fail: min age")).toBeInTheDocument();
    expect(screen.getByText("pass: buy sell ratio")).toBeInTheDocument();
    expect(screen.getByText(/Missing data \(scored as 0\): holder_distribution/)).toBeInTheDocument();
    expect(screen.getByText("TOP10_CONCENTRATION")).toBeInTheDocument();
    expect(screen.getByText("VETO")).toBeInTheDocument();
    expect(screen.getByText("Not consulted.")).toBeInTheDocument();
    expect(screen.getByText("No order was placed.")).toBeInTheDocument();
    expect(screen.getByText("BUY_REJECTED")).toBeInTheDocument();
    expect(screen.getByText("DEMO DATA")).toBeInTheDocument();
  });

  it("labels a simulated fill and states no blockchain transaction happened", async () => {
    const d = decision({ decision: { ...decision().decision, final_action: "BUY", final_reason: "approved" },
      execution: [{ order_id: "paper-1", status: "FILLED", simulated: true, tx_hash: null, avg_price: 1.0015, filled_quantity: 24, fee_usdc: 0.075, slippage_pct: 0.15, error: null }] });
    mockFetch({ "GET /decisions/d1": d });
    renderApp(<DecisionDetail decisionId="d1" />);
    expect(await screen.findByText("PAPER (SIMULATED)")).toBeInTheDocument();
    expect(screen.getByText("no blockchain transaction")).toBeInTheDocument();
  });

  it("shows a not-found error for an unknown decision", async () => {
    mockFetch({ "GET /decisions/nope": () => ({ status: 404, body: { detail: "not found" } }) });
    renderApp(<DecisionDetail decisionId="nope" />);
    expect(await screen.findByText("not found")).toBeInTheDocument();
  });
});

describe("token detail", () => {
  it("shows honest gaps: unverified creator, unknown contract fields, no per-holder list, thin price history", async () => {
    mockFetch({ "GET /tokens/arc%3A0xde04": tokenDetail(), "GET /decisions/d1": decision() });
    renderApp(<TokenDetail tokenKey="arc:0xde04" />);
    expect(await screen.findByText("DEMO-RUG")).toBeInTheDocument();
    expect(screen.getByText(/Creator behaviour could not be verified/)).toBeInTheDocument();
    expect(screen.getByText(/A per-holder list is not shown/)).toBeInTheDocument();
    expect(screen.getByText("Not enough price history yet.")).toBeInTheDocument();
    expect(screen.getAllByText("unknown").length).toBeGreaterThan(3);
    expect(screen.getByText("Decision history")).toBeInTheDocument();
    expect(await screen.findByText(/Score 74.2/)).toBeInTheDocument();
    expect(screen.getByText("missing")).toBeInTheDocument();
  });
});

describe("browser wallet helpers", () => {
  it("reports a missing wallet clearly", async () => {
    await expect(personalSign("0xabc", "hi")).rejects.toBeInstanceOf(WalletError);
  });

  it("hex-encodes the message for personal_sign", async () => {
    const request = vi.fn(async () => "0xsig");
    window.ethereum = { request };
    await personalSign("0xabc", "Hi");
    expect(request).toHaveBeenCalledWith({ method: "personal_sign", params: ["0x4869", "0xabc"] });
  });

  it("adds Arc with the documented native-USDC (18 decimals) parameters when the wallet does not know it", async () => {
    const params = wallet().network.chain_params;
    const request = vi.fn(async ({ method }: { method: string }) => { if (method === "wallet_switchEthereumChain") throw Object.assign(new Error("unknown chain"), { code: 4902 }); return null; });
    window.ethereum = { request };
    await ensureArcNetwork(params);
    expect(request).toHaveBeenLastCalledWith({ method: "wallet_addEthereumChain", params: [params] });
    expect(params.nativeCurrency.decimals).toBe(18);
  });

  it("rethrows a user rejection instead of swallowing it", async () => {
    window.ethereum = { request: vi.fn(async () => { throw Object.assign(new Error("User rejected"), { code: 4001 }); }) };
    await expect(ensureArcNetwork(wallet().network.chain_params)).rejects.toMatchObject({ code: 4001 });
  });

  it("builds eth_sendTransaction for a queued signing request; the backend never signs", async () => {
    const request = vi.fn(async () => "0x" + "ab".repeat(32));
    window.ethereum = { request };
    const hash = await sendSigningRequest("0xfrom", { id: "r1", status: "PENDING", tx_hash: null, created_at: "x", tx: { to: "0xrouter", data: "0xdead", value: 0, chain_id: 5042, amount_usdc: 10, side: "BUY" } });
    expect(hash).toMatch(/^0x[ab]{64}$/);
    expect(request).toHaveBeenCalledWith({ method: "eth_sendTransaction", params: [{ from: "0xfrom", to: "0xrouter", data: "0xdead", value: "0x0", chainId: "0x13b2" }] });
  });
});

describe("formatting", () => {
  it("formats money, percent, price and durations", () => {
    expect(usd(1234.5)).toBe("$1,234.50");
    expect(usd(null)).toBe("—");
    expect(pct(12.34)).toBe("+12.3%");
    expect(pct(-5)).toBe("-5.0%");
    expect(price(0.00001234)).toBe("$0.00001234");
    expect(price(1.23456)).toBe("$1.2346");
    expect(duration(45)).toBe("45s");
    expect(duration(600)).toBe("10m");
    expect(duration(7200)).toBe("2h");
    expect(ago("2026-09-19T12:00:00Z", new Date("2026-09-19T12:05:00Z").getTime())).toBe("5m ago");
  });
});
