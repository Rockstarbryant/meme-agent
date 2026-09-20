import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, setToken, setUnauthorizedHandler } from "@/lib/api";
import { mockFetch } from "./helpers";

beforeEach(() => { setToken(null); setUnauthorizedHandler(null); });

describe("api client", () => {
  it("sends the bearer token and returns parsed JSON", async () => {
    const m = mockFetch({ "GET /auth/me": { id: "u1" } });
    setToken("tok123");
    expect(await api("/auth/me")).toEqual({ id: "u1" });
    const headers = (m.fn.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer tok123");
  });

  it("turns FastAPI string details into readable errors", async () => {
    mockFetch({ "POST /agent/start": () => ({ status: 409, body: { detail: "emergency stop is active; disable it explicitly first" } }) });
    await expect(api("/agent/start", { method: "POST" })).rejects.toMatchObject({ status: 409, message: "emergency stop is active; disable it explicitly first" });
  });

  it("joins validation error arrays", async () => {
    mockFetch({ "PUT /risk/limits": () => ({ status: 422, body: { detail: [{ msg: "Value error, require 0 < max_trade" }, { msg: "bad slippage" }] } }) });
    await expect(api("/risk/limits", { method: "PUT", body: {} })).rejects.toThrow("Value error, require 0 < max_trade; bad slippage");
  });

  it("surfaces LIVE blockers from structured details", async () => {
    mockFetch({ "POST /agent/mode": () => ({ status: 409, body: { detail: { error: "LIVE_NOT_READY", blockers: ["not verified", "no wallet"] } } }) });
    const err = (await api("/agent/mode", { method: "POST" }).catch((e) => e)) as ApiError;
    expect(err.status).toBe(409);
    expect(err.blockers).toEqual(["not verified", "no wallet"]);
  });

  it("logs out on 401 when a token was in use, but not on a failed login", async () => {
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    mockFetch({ "POST /auth/login": () => ({ status: 401, body: { detail: "invalid credentials" } }), "GET /portfolio": () => ({ status: 401, body: { detail: "invalid or expired token" } }) });
    await expect(api("/auth/login", { method: "POST", body: {} })).rejects.toThrow("invalid credentials");
    expect(onUnauthorized).not.toHaveBeenCalled();
    setToken("expired");
    await expect(api("/portfolio")).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("reports an unreachable server clearly", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("fetch failed"); }));
    await expect(api("/health")).rejects.toMatchObject({ status: 0, message: expect.stringContaining("Cannot reach the server") });
  });

  it("never leaks server internals on 5xx and explains rate limits", async () => {
    mockFetch({ "GET /a": () => ({ status: 500, body: { detail: "internal error" } }), "GET /b": () => ({ status: 429, body: {} }) });
    await expect(api("/a")).rejects.toMatchObject({ status: 500, message: "The server had a problem. Please try again." });
    await expect(api("/b")).rejects.toThrow("Too many requests");
  });
});
