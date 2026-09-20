export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

export class ApiError extends Error {
  status: number;
  detail: unknown;
  blockers: string[];
  constructor(status: number, message: string, detail: unknown = null, blockers: string[] = []) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.blockers = blockers;
  }
}

let token: string | null = null;
let onUnauthorized: (() => void) | null = null;
export const setToken = (t: string | null) => { token = t; };
export const getToken = () => token;
export const setUnauthorizedHandler = (fn: (() => void) | null) => { onUnauthorized = fn; };

function describe(status: number, body: unknown): { message: string; blockers: string[] } {
  if (status >= 500) return { message: "The server had a problem. Please try again.", blockers: [] };  // never echo server internals
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return { message: detail, blockers: [] };
  if (Array.isArray(detail)) {  // FastAPI validation errors
    const msg = detail.map((d) => (d as { msg?: string }).msg).filter(Boolean).join("; ");
    return { message: msg || "Invalid request", blockers: [] };
  }
  if (detail && typeof detail === "object") {
    const d = detail as { error?: string; detail?: string; blockers?: string[]; type_exactly?: string };
    return { message: d.detail ?? d.error ?? `Request failed (${status})`, blockers: d.blockers ?? [] };
  }
  if (status === 429) return { message: "Too many requests. Please wait a moment.", blockers: [] };
  if (status >= 500) return { message: "The server had a problem. Please try again.", blockers: [] };
  return { message: `Request failed (${status})`, blockers: [] };
}

export async function api<T>(path: string, opts: { method?: string; body?: unknown; signal?: AbortSignal } = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      method: opts.method ?? "GET",
      headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      signal: opts.signal,
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new ApiError(0, "Cannot reach the server. Check your connection and that the API is running.");
  }
  let body: unknown = null;
  try { body = await res.json(); } catch { /* empty or non-JSON body */ }
  if (!res.ok) {
    if (res.status === 401 && token) onUnauthorized?.();
    const { message, blockers } = describe(res.status, body);
    throw new ApiError(res.status, message, (body as { detail?: unknown } | null)?.detail ?? null, blockers);
  }
  return body as T;
}

export const toApiError = (e: unknown): ApiError =>
  e instanceof ApiError ? e : new ApiError(0, e instanceof Error ? e.message : "Unexpected error");
