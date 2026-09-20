import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { RiskLimitsForm, validateLimits } from "@/components/features/risk-limits-form";
import { limits } from "./fixtures";
import { mockFetch, renderApp, TEST_USER } from "./helpers";

const res = { "GET /risk/limits": { limits: limits(), effective_limits: limits({ max_trade_usdc: 20 }) } };

describe("risk configuration", () => {
  it("loads current limits and explains the effective (stricter) limit", async () => {
    mockFetch(res);
    renderApp(<RiskLimitsForm />);
    expect(await screen.findByLabelText("Maximum trade (USDC)")).toHaveValue(25);
    expect(screen.getByText(/max trade now 20 USDC/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save risk limits" })).toBeDisabled();  // nothing changed
  });

  it("rejects inconsistent values client-side", async () => {
    mockFetch(res);
    renderApp(<RiskLimitsForm />);
    const u = userEvent.setup();
    const f = await screen.findByLabelText("Maximum trade (USDC)");
    await u.clear(f); await u.type(f, "80");
    expect(screen.getByText("Maximum trade cannot exceed maximum position.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save risk limits" })).toBeDisabled();
  });

  it("saves in PAPER mode without an extra confirmation, sending the full limits object", async () => {
    const m = mockFetch({ ...res, "PUT /risk/limits": { limits: limits({ max_trade_usdc: 10 }) } });
    renderApp(<RiskLimitsForm />);
    const u = userEvent.setup();
    const f = await screen.findByLabelText("Maximum trade (USDC)");
    await u.clear(f); await u.type(f, "10");
    await u.click(screen.getByRole("button", { name: "Save risk limits" }));
    await waitFor(() => expect(m.find("PUT", "/risk/limits")).toHaveLength(1));
    expect(m.find("PUT", "/risk/limits")[0].body).toMatchObject({ max_trade_usdc: 10, max_position_usdc: 50, min_liquidity_usdc: 10000, confirm: false });
    expect(await screen.findByText(/restarted in STOPPED state/)).toBeInTheDocument();
  });

  it("requires a confirmation dialog in LIVE mode and sends confirm=true", async () => {
    const m = mockFetch({ ...res, "PUT /risk/limits": { limits: limits() } });
    renderApp(<RiskLimitsForm />, { user: { ...TEST_USER, mode: "LIVE" } });
    const u = userEvent.setup();
    const f = await screen.findByLabelText("Maximum slippage (%)");
    await u.clear(f); await u.type(f, "1");
    await u.click(screen.getByRole("button", { name: "Save risk limits" }));
    expect(m.find("PUT", "/risk/limits")).toHaveLength(0);
    await u.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Save limits" }));
    await waitFor(() => expect(m.find("PUT", "/risk/limits")[0].body).toMatchObject({ max_slippage_pct: 1, confirm: true }));
  });

  it("shows server-side rejections", async () => {
    mockFetch({ ...res, "PUT /risk/limits": () => ({ status: 422, body: { detail: [{ msg: "Value error, require 0 < max_trade <= max_position" }] } }) });
    renderApp(<RiskLimitsForm />);
    const u = userEvent.setup();
    const f = await screen.findByLabelText("Minimum liquidity (USDC)");
    await u.clear(f); await u.type(f, "5000");
    await u.click(screen.getByRole("button", { name: "Save risk limits" }));
    expect(await screen.findByText(/Value error, require 0 < max_trade/)).toBeInTheDocument();
  });

  it("validateLimits", () => {
    const l = { max_trade_usdc: 25, max_position_usdc: 50, max_daily_loss_usdc: 50, max_total_exposure_usdc: 250, max_open_positions: 5, max_slippage_pct: 2, min_liquidity_usdc: 10000 };
    expect(validateLimits(l)).toBeNull();
    expect(validateLimits({ ...l, max_position_usdc: 300 })).toMatch(/total exposure/);
    expect(validateLimits({ ...l, max_daily_loss_usdc: 0 })).toMatch(/greater than zero/);
  });
});
