import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LoginForm } from "@/components/login-form";
import { AuthProvider } from "@/lib/auth";
import { getToken, setToken } from "@/lib/api";
import { mockFetch, TEST_USER } from "./helpers";

const replace = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }), usePathname: () => "/", useParams: () => ({}) }));

const ok = { access_token: "jwt-abc", token_type: "bearer", expires_in: 3600, user: TEST_USER };
beforeEach(() => { replace.mockClear(); setToken(null); });

async function fill(email: string, password: string) {
  const u = userEvent.setup();
  await u.type(screen.getByLabelText("Email"), email);
  await u.type(screen.getByLabelText("Password"), password);
  return u;
}

describe("authentication", () => {
  it("logs in, keeps the token in session storage (not the URL) and navigates home", async () => {
    const m = mockFetch({ "POST /auth/login": ok });
    render(<AuthProvider><LoginForm /></AuthProvider>);
    const u = await fill("test@example.com", "correct horse battery");
    await u.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
    expect(m.find("POST", "/auth/login")[0].body).toEqual({ email: "test@example.com", password: "correct horse battery" });
    expect(sessionStorage.getItem("arc_agent_token")).toBe("jwt-abc");
    expect(getToken()).toBe("jwt-abc");
  });

  it("shows the server error and does not navigate when credentials are wrong", async () => {
    mockFetch({ "POST /auth/login": () => ({ status: 401, body: { detail: "invalid credentials" } }) });
    render(<AuthProvider><LoginForm /></AuthProvider>);
    const u = await fill("test@example.com", "wrong password!!");
    await u.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("invalid credentials")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
    expect(sessionStorage.getItem("arc_agent_token")).toBeNull();
  });

  it("enforces the password length on registration before any network call", async () => {
    const m = mockFetch({});
    render(<AuthProvider><LoginForm /></AuthProvider>);
    const u = userEvent.setup();
    await u.click(screen.getByRole("button", { name: /need an account/i }));
    await u.type(screen.getByLabelText("Email"), "new@example.com");
    await u.type(screen.getByLabelText("Password"), "short");
    await u.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByText(/at least 10 characters/)).toBeInTheDocument();
    expect(m.calls.filter((c) => c.path === "/auth/register")).toHaveLength(0);
  });

  it("states that new accounts start in PAPER with LIVE off", async () => {
    mockFetch({});
    render(<AuthProvider><LoginForm /></AuthProvider>);
    expect(await screen.findByText(/start in PAPER mode/i)).toBeInTheDocument();
  });
});
