"use client";
import Link from "next/link";
import { ActionBadge, DataLabel } from "@/components/badges";
import { ErrorState, Loading, Stat } from "@/components/states";
import { PriceChart } from "@/components/price-chart";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useApi } from "@/hooks/use-api";
import { compact, num, plainPct, price } from "@/lib/format";
import type { DecisionDetail, TokenDetail as TD } from "@/types/api";

const tri = (v: unknown) => (v === true ? "yes" : v === false ? "no" : "unknown");
const n = (m: Record<string, unknown>, k: string) => (typeof m[k] === "number" ? (m[k] as number) : null);

export function TokenDetail({ tokenKey }: { tokenKey: string }) {
  const res = useApi<TD>(`/tokens/${encodeURIComponent(tokenKey)}`, { refreshOn: ["DECISION_RECORDED"] });
  const did = res.data?.latest_decision?.decision_id;
  const dec = useApi<DecisionDetail>(did ? `/decisions/${did}` : null);
  if (res.loading && !res.data) return <Loading />;
  if (!res.data) return res.error ? <ErrorState error={res.error} onRetry={() => void res.reload()} /> : null;
  const t = res.data, m = t.latest_market, c = (m.contract ?? {}) as Record<string, unknown>;
  const sig = dec.data?.strategy.signal;
  const ai = dec.data?.ai[0];
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2"><h2 className="text-lg font-semibold">{(m.symbol as string) ?? t.token_key}</h2><DataLabel label={t.data_label} />
        {t.latest_decision && <ActionBadge action={t.latest_decision.final_action} />}<span className="break-all text-xs text-muted-foreground">{t.token_key}</span></div>
      <Card><CardContent className="space-y-3 pt-4"><div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Stat label="Price" value={price(n(m, "price"))} /><Stat label="Market cap" value={compact(n(m, "market_cap"))} /><Stat label="Liquidity" value={compact(n(m, "liquidity"))} />
        <Stat label="Volume 1m / 5m / 15m" value={`${compact(n(m, "volume_1m"))} / ${compact(n(m, "volume_5m"))} / ${compact(n(m, "volume_15m"))}`} /></div>
        <PriceChart points={t.price_series} /></CardContent></Card>
      <div className="grid gap-4 md:grid-cols-2">
        <Card><CardHeader><CardTitle>Holders</CardTitle></CardHeader><CardContent className="space-y-1 text-sm">
          <p>Holder count: {n(m, "holder_count") ?? "—"} (growth {plainPct(n(m, "holder_growth_pct"))})</p>
          <p>Top 5 / 10 / 20 concentration: {plainPct(n(m, "top5_holder_pct"))} / {plainPct(n(m, "top10_holder_pct"))} / {plainPct(n(m, "top20_holder_pct"))}</p>
          <p className="text-xs text-muted-foreground">A per-holder list is not shown: no holder-indexing source is configured, so only aggregate concentration is available when a data source provides it.</p></CardContent></Card>
        <Card><CardHeader><CardTitle>Buy and sell activity</CardTitle></CardHeader><CardContent className="space-y-1 text-sm">
          <p>Unique buyers 1m / 5m: {n(m, "unique_buyers_1m") ?? "—"} / {n(m, "unique_buyers_5m") ?? "—"}</p>
          <p>Unique sellers 1m / 5m: {n(m, "unique_sellers_1m") ?? "—"} / {n(m, "unique_sellers_5m") ?? "—"}</p>
          <p>Buy vs sell volume 5m: {compact(n(m, "buy_volume_5m"))} / {compact(n(m, "sell_volume_5m"))}</p></CardContent></Card>
        <Card><CardHeader><CardTitle>Creator</CardTitle></CardHeader><CardContent className="space-y-1 text-sm">
          {m.creator_known ? <><p>Balance: {plainPct(n(m, "creator_balance_pct"))}</p><p>Sold: {plainPct(n(m, "creator_sold_pct"))}</p></> : <p>Creator behaviour could not be verified. It is not assumed to be safe.</p>}</CardContent></Card>
        <Card><CardHeader><CardTitle>Contract risk</CardTitle></CardHeader><CardContent><dl className="grid grid-cols-2 gap-1 text-sm">
          {(["verified", "owner_renounced", "mint_authority_active", "pausable", "blacklist_capability", "transfer_restricted", "is_proxy", "sell_simulation_ok"] as const).map((k) => (
            <div key={k} className="flex justify-between gap-2"><dt className="text-muted-foreground">{k.replaceAll("_", " ")}</dt><dd>{tri(c[k])}</dd></div>))}
          <div className="flex justify-between gap-2"><dt className="text-muted-foreground">buy / sell tax</dt><dd>{plainPct(typeof c.buy_tax_pct === "number" ? c.buy_tax_pct : null)} / {plainPct(typeof c.sell_tax_pct === "number" ? c.sell_tax_pct : null)}</dd></div></dl></CardContent></Card>
        <Card><CardHeader><CardTitle>Execution risk</CardTitle></CardHeader><CardContent className="space-y-1 text-sm">
          <p>Expected price impact: {plainPct(n(m, "expected_price_impact_pct"), 2)}</p><p>MEV risk score: {num(n(m, "mev_risk_score"))}</p></CardContent></Card>
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
