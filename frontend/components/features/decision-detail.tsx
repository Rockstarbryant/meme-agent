"use client";
import { ActionBadge, DataLabel, SimulatedLabel } from "@/components/badges";
import { ErrorState, Loading } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useApi } from "@/hooks/use-api";
import { num, price, usd } from "@/lib/format";
import type { DecisionDetail as DD } from "@/types/api";

const SEV = { VETO: "destructive", WARN: "warning", INFO: "default" } as const;

/** "Why did the agent buy (or not buy) this token?" rebuilt from the audit trail. */
export function DecisionDetail({ decisionId }: { decisionId: string }) {
  const res = useApi<DD>(`/decisions/${decisionId}`);
  if (res.loading && !res.data) return <Loading />;
  if (!res.data) return res.error ? <ErrorState error={res.error} onRetry={() => void res.reload()} /> : null;
  const d = res.data, sig = d.strategy.signal, saw = d.what_the_agent_saw as Record<string, unknown>;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2"><ActionBadge action={d.decision.final_action} /><h2 className="text-lg font-semibold">{d.decision.token_key}</h2><DataLabel label={d.decision.data_label} /><Badge>{d.decision.mode}</Badge></div>
      <p className="text-sm">{d.decision.final_reason}</p>
      <Card><CardHeader><CardTitle>1. What the agent saw</CardTitle></CardHeader><CardContent className="grid grid-cols-2 gap-2 text-sm md:grid-cols-4">
        {(["price", "liquidity", "volume_5m", "unique_buyers_5m", "top10_holder_pct", "price_change_5m"] as const).map((k) => <div key={k}><p className="text-xs text-muted-foreground">{k.replaceAll("_", " ")}</p><p>{typeof saw[k] === "number" ? (k === "price" ? price(saw[k] as number) : num(saw[k] as number)) : "—"}</p></div>)}</CardContent></Card>
      <Card><CardHeader><CardTitle>2. Strategy: {d.strategy.id} v{d.strategy.version}</CardTitle></CardHeader><CardContent className="space-y-2 text-sm">
        {sig ? (<><p>Score <strong>{num(sig.score, 1)}</strong> (needs {String((d.strategy.config_snapshot as { min_score?: number }).min_score ?? "?")}) · {sig.qualified ? "qualified" : "not qualified"}</p>
          <div className="flex flex-wrap gap-1">{Object.entries(sig.gates).map(([k, ok]) => <Badge key={k} variant={ok ? "success" : "destructive"}>{ok ? "pass" : "fail"}: {k.replaceAll("_", " ")}</Badge>)}</div>
          {sig.data_gaps.length > 0 && <p className="text-xs text-muted-foreground">Missing data (scored as 0): {sig.data_gaps.join(", ")}</p>}</>) : <p>No signal.</p>}</CardContent></Card>
      <Card><CardHeader><CardTitle>3. Risk engine</CardTitle></CardHeader><CardContent className="space-y-3 text-sm">
        {d.risk.assessments.map((a) => (<div key={a.stage}><p className="font-medium">{a.stage === "PRE" ? "Pre-filter" : "Final check"}: {a.decision} (risk score {num(a.risk_score, 0)})</p>
          <ul className="mt-1 space-y-1">{a.flags.filter((f) => f.severity !== "INFO").map((f) => <li key={f.rule} className="flex flex-wrap items-center gap-2"><Badge variant={SEV[f.severity]}>{f.severity}</Badge><span className="font-mono text-xs">{f.rule}</span><span className="text-xs text-muted-foreground">{f.message}</span></li>)}
            {a.flags.filter((f) => f.severity !== "INFO").length === 0 && <li className="text-xs text-muted-foreground">No warnings or vetoes.</li>}</ul></div>))}</CardContent></Card>
      <Card><CardHeader><CardTitle>4. AI</CardTitle></CardHeader><CardContent className="space-y-1 text-sm">
        {d.ai.length === 0 ? <p className="text-muted-foreground">Not consulted.</p> : d.ai.map((a, i) => <div key={i}><p>{a.provider} / {a.model} · prompt {a.prompt_version} · {a.status}</p>
          {a.response && <><p><ActionBadge action={a.response.action} /> confidence {num(a.response.confidence)}</p><p>{a.response.reasoning_summary}</p></>}</div>)}</CardContent></Card>
      <Card><CardHeader><CardTitle>5. Sizing and execution</CardTitle></CardHeader><CardContent className="space-y-2 text-sm">
        <p>Approved size: {usd(d.sized_amount_usdc)}</p>
        {d.execution.length === 0 && <p className="text-muted-foreground">No order was placed.</p>}
        {d.execution.map((e) => <div key={e.order_id} className="flex flex-wrap items-center gap-2"><SimulatedLabel simulated={e.simulated} /><span>{e.status}</span><span>fill {price(e.avg_price)}</span><span>fee {usd(e.fee_usdc)}</span>
          {e.tx_hash ? <code className="text-xs">{e.tx_hash}</code> : <span className="text-xs text-muted-foreground">no blockchain transaction</span>}</div>)}
        {d.positions.map((p) => <p key={p.id}>Position {p.status}: P&L {usd(p.realized_pnl_usdc + p.unrealized_pnl_usdc)}{p.exit_reason ? ` · exit ${p.exit_reason}` : ""}</p>)}</CardContent></Card>
      <Card><CardHeader><CardTitle>6. Timeline</CardTitle></CardHeader><CardContent><ol className="space-y-1 text-xs">{d.events.map((e, i) => <li key={i}><span className="text-muted-foreground">{new Date(e.at).toLocaleTimeString()}</span> {e.type}</li>)}</ol></CardContent></Card>
    </div>
  );
}
