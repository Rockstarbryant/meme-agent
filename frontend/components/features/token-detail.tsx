"use client";
import Link from "next/link";
import { ActionBadge, DataLabel } from "@/components/badges";
import { ErrorState, Loading, Stat } from "@/components/states";
import { PriceChart } from "@/components/price-chart";
import { HoldersCard, MarketWindowsCard } from "@/components/market-windows";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useApi } from "@/hooks/use-api";
import { ago, compact, num, plainPct, price } from "@/lib/format";
import type { DecisionDetail, TokenDetail as TD } from "@/types/api";

const tri = (v: unknown) => (v === true ? "yes" : v === false ? "no" : "unknown");
const n = (m: Record<string, unknown>, k: string) => (typeof m[k] === "number" ? (m[k] as number) : null);
const winVol = (m: Record<string, unknown>, w: string) =>
  ((m.windows as Record<string, { volume_usd?: number | null }> | undefined)?.[w]?.volume_usd ?? null);

export function TokenDetail({ tokenKey }: { tokenKey: string }) {
  const res = useApi<TD>(`/tokens/${encodeURIComponent(tokenKey)}`, { refreshOn: ["DECISION_RECORDED"] });
  const did = res.data?.latest_decision?.decision_id;
  const dec = useApi<DecisionDetail>(did ? `/decisions/${did}` : null);
  if (res.loading && !res.data) return <Loading />;
  if (!res.data) return res.error ? <ErrorState error={res.error} onRetry={() => void res.reload()} /> : null;
  const t = res.data, m = t.latest_market, c = (m.contract ?? {}) as Record<string, unknown>;
  const sig = dec.data?.strategy.signal;
  const ai = dec.data?.ai[0];
  const launchpad = (m.launchpad as string | null) ?? (m.launchpad_detected as string | null);
  const launchpadDetected = !m.launchpad && m.launchpad_detected;
  const gaps = (m.enrichment_gaps as string[] | undefined) ?? [];
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2"><h2 className="text-lg font-semibold">{(m.token_name as string) ?? (m.symbol as string) ?? t.token_key}</h2>
        {m.token_name && m.symbol ? <span className="text-sm text-muted-foreground">{m.symbol as string}</span> : null}<DataLabel label={t.data_label} />
        {t.latest_decision && <ActionBadge action={t.latest_decision.final_action} />}<span className="break-all text-xs text-muted-foreground">{t.token_key}</span></div>
      <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
        <span>Launchpad: {launchpad ?? "unknown"}{launchpadDetected ? ` (detected, ${(m.launchpad_evidence as string) ?? "on-chain"})` : ""}</span>
        <span>Scanned: {ago((m.scanned_at as string) ?? (m.enriched_at as string) ?? null)}</span>
      </div>
      {gaps.length > 0 && <p className="text-xs text-muted-foreground">Not enriched: {gaps.join("; ")}</p>}
      <Card><CardContent className="space-y-3 pt-4"><div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Stat label="Price" value={price(n(m, "price"))} /><Stat label="Market cap" value={compact(n(m, "market_cap"))} /><Stat label="Liquidity" value={compact(n(m, "liquidity"))} />
        <Stat label="Volume 5m / 1h / 24h" value={`${compact(n(m, "volume_5m"))} / ${compact(winVol(m, "1h"))} / ${compact(winVol(m, "24h"))}`} /></div>
        <PriceChart points={t.price_series} /></CardContent></Card>
      {t.evaluated === false && <Alert>{t.note ?? "This token has not been evaluated by your agent yet."}</Alert>}
      <MarketWindowsCard market={m} />
      <HoldersCard market={m} />
      <div className="grid gap-4 md:grid-cols-2">
        <Card><CardHeader><CardTitle>Creator</CardTitle></CardHeader><CardContent className="space-y-1 text-sm">
          {m.creator_known ? <><p>Balance: {plainPct(n(m, "creator_balance_pct"))}</p><p>Sold: {plainPct(n(m, "creator_sold_pct"))}</p></> : <p>Creator behaviour could not be verified. It is not assumed to be safe.</p>}</CardContent></Card>
        <Card><CardHeader><CardTitle>Contract risk</CardTitle></CardHeader><CardContent><dl className="grid grid-cols-2 gap-1 text-sm">
          {(["verified", "owner_renounced", "mint_authority_active", "pausable", "blacklist_capability", "transfer_restricted", "is_proxy", "sell_simulation_ok"] as const).map((k) => (
            <div key={k} className="flex justify-between gap-2"><dt className="text-muted-foreground">{k.replaceAll("_", " ")}</dt><dd>{tri(c[k])}</dd></div>))}
          <div className="flex justify-between gap-2"><dt className="text-muted-foreground">buy / sell tax</dt><dd>{plainPct(typeof c.buy_tax_pct === "number" ? c.buy_tax_pct : null)} / {plainPct(typeof c.sell_tax_pct === "number" ? c.sell_tax_pct : null)}</dd></div></dl>
          <p className="mt-2 text-xs text-muted-foreground">
            verification: {(c.verification_source as string) ?? "unavailable"}. Contract findings are informational once both buyers and sellers have been seen trading; sellability is judged from observed sellers (see the trading table above).
          </p></CardContent></Card>
        <Card><CardHeader><CardTitle>Execution risk</CardTitle></CardHeader><CardContent className="space-y-1 text-sm">
          <p>Expected price impact: {plainPct(n(m, "expected_price_impact_pct"), 2)}</p>
          <p>MEV risk score: {num(n(m, "mev_risk_score"))}{m.mev_method ? ` (${m.mev_method as string} — heuristic estimate, not a mempool simulation)` : ""}</p></CardContent></Card>
        <Card><CardHeader><CardTitle>Strategy analysis</CardTitle></CardHeader><CardContent className="space-y-1 text-sm">
          {sig ? (<><p>Score {num(sig.score, 1)} · {sig.qualified ? <Badge variant="success">qualified</Badge> : <Badge variant="warning">not qualified</Badge>}</p>
            {Object.entries(sig.components).map(([k, v]) => <div key={k} className="flex justify-between"><span className="text-muted-foreground">{k.replaceAll("_", " ")}</span><span>{v == null ? "missing" : num(v, 0)}</span></div>)}</>) : <p className="text-muted-foreground">No analysis yet.</p>}</CardContent></Card>
      </div>
      <Card><CardHeader><CardTitle>AI analysis</CardTitle></CardHeader><CardContent className="space-y-1 text-sm">
        {ai ? (ai.response ? (<><p><ActionBadge action={ai.response.action} /> confidence {num(ai.response.confidence)} ({ai.provider} / {ai.model}, prompt {ai.prompt_version})</p><p>{ai.response.reasoning_summary}</p></>) : <p>AI status: {ai.status}{ai.error ? ` (${ai.error})` : ""}</p>)
          : <p className="text-muted-foreground">AI was not consulted (deterministic filters ended the evaluation or AI is disabled).</p>}</CardContent></Card>
      <Card><CardHeader><CardTitle>Decision history</CardTitle></CardHeader><CardContent className="space-y-1">
        {t.decision_history.map((d) => <div key={d.decision_id} className="flex flex-wrap items-center gap-2 text-sm"><ActionBadge action={d.final_action} /><span className="text-xs text-muted-foreground">{new Date(d.at).toLocaleTimeString()}</span>
          <span className="text-xs">{d.final_reason.slice(0, 80)}</span><Link className="ml-auto text-xs underline" href={`/decisions/${d.decision_id}`}>details</Link></div>)}</CardContent></Card>
    </div>
  );
}
