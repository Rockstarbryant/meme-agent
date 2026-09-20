"use client";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, setToken, setUnauthorizedHandler } from "@/lib/api";
import type { AuthResponse, User } from "@/types/api";

const KEY = "arc_agent_token";  // sessionStorage: cleared when the tab closes. Token is never placed in the URL.

export interface AuthCtx {
  user: User | null; ready: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}
export const AuthContext = createContext<AuthCtx | null>(null);

export function useAuth(): AuthCtx {
  const c = useContext(AuthContext);
  if (!c) throw new Error("useAuth must be used inside AuthProvider");
  return c;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  const clear = useCallback(() => { setToken(null); sessionStorage.removeItem(KEY); setUser(null); }, []);
  const accept = useCallback((r: AuthResponse) => { setToken(r.access_token); sessionStorage.setItem(KEY, r.access_token); setUser(r.user); }, []);

  useEffect(() => {
    setUnauthorizedHandler(clear);
    const saved = sessionStorage.getItem(KEY);
    if (!saved) { queueMicrotask(() => setReady(true)); return () => setUnauthorizedHandler(null); }
    setToken(saved);
    api<User>("/auth/me").then(setUser).catch(clear).finally(() => setReady(true));
    return () => setUnauthorizedHandler(null);
  }, [clear]);

  const value = useMemo<AuthCtx>(() => ({
    user, ready,
    login: async (email, password) => accept(await api<AuthResponse>("/auth/login", { method: "POST", body: { email, password } })),
    register: async (email, password) => accept(await api<AuthResponse>("/auth/register", { method: "POST", body: { email, password } })),
    logout: async () => { try { await api("/auth/logout", { method: "POST" }); } catch { /* token may already be invalid */ } clear(); },
    refresh: async () => { setUser(await api<User>("/auth/me")); },
  }), [user, ready, accept, clear]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
