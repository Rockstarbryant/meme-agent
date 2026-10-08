"use client";
import { useEffect, useState } from "react";
import { ActionBadge } from "@/components/badges";
import { useToast } from "@/components/toast";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, toApiError } from "@/lib/api";
import { interpretRetryAi, waitForCommand } from "@/lib/commands";
import { duration, num } from "@/lib/format";
import type { DecisionDetail as DD } from "@/types/api";

/** Must match AI_RETRY_WINDOW_MIN in backend/app/api/routes_trading.py. */
export const AI_RETRY_WINDOW_MIN = 15;

export function aiFailed(d: DD): boolean {
  return d.ai.some((a) => a.status === "UNAVAILABLE" || a.status === "INVALID") || (d.ai.length === 0 && d.decision.final_reason.includes("AI_UNAVAILABLE"));
}

/** AI step of a decision, with a Retry button when the AI failed to assess the token at scan time. */
export function AiCard({ d, onRetried }: { d: DD; onRetried: () => void | Promise<void> }) {
  const { toast, suppressAuto } = useToast();
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 15000); return () => clearInterval(t); }, []);
  const ageS = Math.max(0, (now - new Date(d.decision.at).getTime()) / 1000);
  const failed = aiFailed(d);
  const leftS = AI_RETRY_WINDOW_MIN * 60 - ageS;
  const canRetry = failed && leftS > 0;

  async function retry() {
    setBusy(true);
    const id = `retry-ai-${d.decision.id}`;
    try {
      const q = await api<{ command_id: string }>(`/decisions/${d.decision.id}/retry-ai`, { method: "POST", body: {} });
      suppressAuto(60_000);
      toast({ id, kind: "loading", title: "Retrying the AI assessment", description: "Your runner is fetching current data and asking the AI again…" });
      const out = interpretRetryAi(await waitForCommand(q.command_id, { timeoutMs: 120_000 }));
      if (out.kind === "done") {
        toast({ id, kind: "success", title: `AI assessment finished${out.action ? `: ${out.action}` : ""}`, description: "A new decision was recorded for this token.", href: "/opportunities", linkLabel: "Opportunities" });
        await onRetried();
      } else if (out.kind === "failed") toast({ id, kind: "error", title: "AI retry did not run", description: out.detail });
      else toast({ id, kind: "warning", title: "Still waiting for your runner", description: "No answer yet. Check the Agent page." });
    } catch (e) {
      toast({ id, kind: "error", title: "Cannot retry the AI", description: toApiError(e).message });
    } finally { setBusy(false); suppressAuto(3_000); }
  }

  return (
    <Card><CardHeader><CardTitle>4. AI</CardTitle></CardHeader><CardContent className="space-y-2 text-sm">
      {d.ai.length === 0 ? <p className="text-muted-foreground">Not consulted.</p> : d.ai.map((a, i) => (
        <div key={i} className="space-y-1">
          <p>{a.provider || "no provider"} / {a.model || "no model"} · prompt {a.prompt_version || "n/a"} · {a.status}</p>
          {a.error && <p className="break-words text-xs text-muted-foreground">{a.error}</p>}
          {a.response && <>
            <p><ActionBadge action={a.response.action} /> <span title="The model's own estimate (0 to 1) that this action is the right call. It is not a measure of how bullish it is, and 1.0 would mean certainty.">confidence in {a.response.action}: {num(a.response.confidence)}</span></p>
            <p>{a.response.reasoning_summary}</p></>}
        </div>))}
      {failed && (
        <div className="rounded-md border border-l-[3px] border-l-warning p-3.5">
          <p className="text-sm font-medium">The AI could not assess this token when it was scanned.</p>
          {canRetry ? (
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <Button size="sm" disabled={busy} onClick={() => void retry()}>{busy ? "Retrying…" : "Retry AI"}</Button>
              <span className="text-xs text-muted-foreground">Available for {duration(leftS)} more (scans older than {AI_RETRY_WINDOW_MIN} minutes use stale data).</span>
            </div>
          ) : (
            <p className="mt-1 text-xs text-muted-foreground">This scan is {duration(ageS)} old, past the {AI_RETRY_WINDOW_MIN}-minute retry window. The agent re-scans this token on its own.</p>
          )}
        </div>
      )}
    </CardContent></Card>
  );
}
