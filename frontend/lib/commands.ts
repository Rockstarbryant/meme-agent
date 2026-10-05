import { api } from "@/lib/api";

export interface CommandResult { status: "PENDING" | "DONE" | "FAILED"; detail: string; timedOut?: boolean }

/** Poll a queued runner command until the runner acknowledges it (or we give up waiting). */
export async function waitForCommand(id: string, opts: { timeoutMs?: number; intervalMs?: number; signal?: AbortSignal } = {}): Promise<CommandResult> {
  const timeoutMs = opts.timeoutMs ?? 90_000;
  const intervalMs = opts.intervalMs ?? 2_000;
  const started = Date.now();
  let last: CommandResult = { status: "PENDING", detail: "" };
  while (Date.now() - started < timeoutMs) {
    if (opts.signal?.aborted) break;
    try {
      const c = await api<{ status: CommandResult["status"]; detail: string }>(`/agent/commands/${id}`);
      last = { status: c.status, detail: c.detail ?? "" };
      if (c.status !== "PENDING") return last;
    } catch { /* transient network error: keep polling */ }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  return { ...last, timedOut: true };
}

export type ForceBuyOutcome =
  | { kind: "opened"; detail: string }
  | { kind: "pending"; detail: string }       // order accepted but not filled yet (e.g. waiting for a signature)
  | { kind: "veto"; reason: string }
  | { kind: "failed"; detail: string }
  | { kind: "timeout" };

/** Interpret the runner's result text for a FORCE_BUY command (see runner/runtime.py _handle_force_buy). */
export function interpretForceBuy(r: CommandResult): ForceBuyOutcome {
  if (r.timedOut && r.status === "PENDING") return { kind: "timeout" };
  const d = r.detail ?? "";
  if (r.status === "FAILED") return { kind: "failed", detail: d || "The runner could not place the order." };
  const veto = d.match(/^risk veto:\s*(.*?)\.\s*The risk engine/i);
  if (veto || d.toLowerCase().includes("vetoed")) return { kind: "veto", reason: (veto?.[1] || "see Activity for the reason").trim() };
  const st = d.match(/order status=(\w+)/i)?.[1]?.toUpperCase();
  if (st === "FILLED" || st === "PARTIALLY_FILLED") return { kind: "opened", detail: st === "FILLED" ? "Order filled." : "Order partially filled." };
  if (st === "SUBMITTED" || st === "PENDING_SIGNATURE") return { kind: "pending", detail: st === "PENDING_SIGNATURE" ? "Waiting for your wallet to sign." : "Order submitted, waiting for the fill." };
  if (st) return { kind: "failed", detail: `Order ${st.toLowerCase().replaceAll("_", " ")}.` };
  return { kind: "failed", detail: d || "Unknown result from the runner." };
}

export type RetryOutcome = { kind: "done"; action: string; detail: string } | { kind: "failed"; detail: string } | { kind: "timeout" };
export function interpretRetryAi(r: CommandResult): RetryOutcome {
  if (r.timedOut && r.status === "PENDING") return { kind: "timeout" };
  if (r.status === "FAILED") return { kind: "failed", detail: r.detail || "The runner could not retry the AI step." };
  const m = (r.detail ?? "").match(/re-evaluated:\s*(\w+)/i);
  return { kind: "done", action: (m?.[1] ?? "").toUpperCase(), detail: r.detail };
}
