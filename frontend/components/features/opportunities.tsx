"use client";
import { useMemo, useState } from "react";
import Link from "next/link";
import { ActionBadge, DataLabel } from "@/components/badges";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { Alert } from "@/components/ui/alert";
import { Empty, ErrorState, Loading } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useApi } from "@/hooks/use-api";
import { api, toApiError, type ApiError } from "@/lib/api";
import { ago, compact, duration, num, plainPct, price, shortAddr } from "@/lib/format";
import type { Action, Opportunity } from "@/types/api";

const FILTERS: (Action | "ALL")[] = ["ALL", "BUY", "WATCH", "REJECT"];

// Client-side category thresholds. These are UI browsing filters, independent of (and looser than) the
// runner's own established-token scanning thresholds (ARC_RUNNER_ESTABLISHED_MIN_* in settings) -- the runner
// decides what to trade, this just helps you browse what it already found. Tune freely.
const NEW_MAX_AGE_S = 2 * 3600;        // matches the runner's lowered 2h established-age default
const HIGH_MC_USDC = 100_000;
const HIGH_LIQUIDITY_USDC = 50_000;
const MOMENTUM_MIN_BUY_SELL_RATIO = 1.2;

type Category = "ALL" | "NEW" | "MOMENTUM" | "HIGH_MC" | "HIGH_LIQUIDITY" | "GAINERS" | "LOSERS";
const CATEGORIES: { key: Category; label: string; title: string }[] = [
  { key: "ALL", label: "All", title: "No category filter" },
  { key: "NEW", label: "New", title: `Age under ${NEW_MAX_AGE_S / 3600}h` },
  { key: "MOMENTUM", label: "Momentum", title: "Rising price (5m/15m), rising holder count, or buy-heavy volume" },
  { key: "HIGH_MC", label: "High MC", title: `Market cap ≥ $${HIGH_MC_USDC.toLocaleString()}` },
  { key: "HIGH_LIQUIDITY", label: "High liquidity", title: `Liquidity ≥ $${HIGH_LIQUIDITY_USDC.toLocaleString()}` },
  { key: "GAINERS", label: "Gainers", title: "Positive 5m price change, sorted highest first" },
  { key: "LOSERS", label: "Losers", title: "Negative 5m price change, sorted lowest first" },
];

function hasMomentum(o: Opportunity): boolean {
  if ((o.price_change_5m ?? 0) > 0 || (o.price_change_15m ?? 0) > 0) return true;
  if ((o.holder_growth_pct ?? 0) > 0) return true;
  if (o.buy_sell_ratio != null && o.buy_sell_ratio >= MOMENTUM_MIN_BUY_SELL_RATIO) return true;
  return false;
}

function matchesCategory(o: Opportunity, cat: Category): boolean {
  switch (cat) {
    case "ALL": return true;
    case "NEW": return o.age_seconds != null && o.age_seconds < NEW_MAX_AGE_S;
    case "MOMENTUM": return hasMomentum(o);
    case "HIGH_MC": return (o.market_cap ?? 0) >= HIGH_MC_USDC;
    case "HIGH_LIQUIDITY": return (o.liquidity ?? 0) >= HIGH_LIQUIDITY_USDC;
    case "GAINERS": return (o.price_change_5m ?? 0) > 0;
    case "LOSERS": return (o.price_change_5m ?? 0) < 0;
  }
}

const Cell = ({ label, value, title }: { label: string; value: React.ReactNode; title?: string }) => (
  <div title={title}><dt className="text-[11px] text-muted-foreground">{label}</dt><dd className="text-sm tabular-nums">{value}</dd></div>
);

export function OpportunityCard({ o, onBuyAnyway }: { o: Opportunity; onBuyAnyway?: (o: Opportunity) => void }) {
  const creator = o.creator_known ? (o.creator_sold_pct != null ? `sold ${plainPct(o.creator_sold_pct)}` : "known") : "unverified";
  const launchpad = o.launchpad ?? o.launchpad_detected;
  const launchpadTitle = !o.launchpad && o.launchpad_detected
    ? `Detected from creation tx (${o.launchpad_evidence ?? "on-chain"}); not confirmed against the launchpad allowlist`
    : undefined;
  const ratioTitle = o.buy_sell_basis === "usd" ? "From real buy/sell USD volume"
    : o.buy_sell_basis === "estimated_from_counts" ? "Estimated: total volume split by buy/sell transaction counts"
    : o.buy_sell_basis === "counts" ? "From buy/sell transaction counts only (no USD volume available)" : undefined;
  return (
    <Card><CardContent className="space-y-2 pt-4">
      <div className="flex flex-wrap items-center gap-2">
        <Link className="font-semibold underline" href={`/tokens/${encodeURIComponent(o.token_key)}`}>{o.symbol ?? shortAddr(o.token_key)}</Link>
        <span className="text-xs text-muted-foreground" title={launchpadTitle}>
          {o.chain}{launchpad ? ` · ${launchpad}${launchpadTitle ? " (detected)" : ""}` : ""}
        </span>
        <ActionBadge action={o.final_action} /><DataLabel label={o.data_label} />
        <Link className="ml-auto text-xs underline" href={`/decisions/${o.decision_id}`}>why?</Link>
      </div>
      <dl className="grid grid-cols-3 gap-2 md:grid-cols-6 lg:grid-cols-7">
        <Cell label="Age" value={duration(o.age_seconds)} /><Cell label="Scanned" value={ago(o.scanned_at)} title="When our scanner last refreshed this token's data" />
        <Cell label="Price" value={price(o.price)} /><Cell label="Market cap" value={compact(o.market_cap)} />
        <Cell label="Liquidity" value={compact(o.liquidity)} /><Cell label="Volume 5m" value={compact(o.volume_5m)} /><Cell label="Buyers 5m" value={o.unique_buyers_5m ?? "—"} />
        <Cell label="Price Δ5m" value={o.price_change_5m != null ? `${o.price_change_5m > 0 ? "+" : ""}${num(o.price_change_5m, 2)}%` : "—"} />
        <Cell label="Buy/sell" value={o.buy_sell_ratio != null && o.buy_sell_basis && o.buy_sell_basis !== "usd" ? `~${num(o.buy_sell_ratio)}` : num(o.buy_sell_ratio)} title={ratioTitle} />
        <Cell label="Holder growth" value={plainPct(o.holder_growth_pct)} /><Cell label="Top-10 holders" value={plainPct(o.top10_holder_pct)} />
        <Cell label="Creator" value={creator} /><Cell label="Strategy score" value={num(o.strategy_score, 1)} /><Cell label="Risk score" value={num(o.risk_score, 0)} />
        <Cell label="AI" value={o.ai_status} />
        <Cell label="MEV" value={o.mev_risk_score != null ? num(o.mev_risk_score, 2) : "—"} title={o.mev_method ? `Heuristic estimate (${o.mev_method}), not a mempool simulation` : undefined} />
      </dl>
      <p className="text-xs text-muted-foreground">{o.final_reason}</p>
      {o.enrichment_gaps.length > 0 && (
        <p className="text-[11px] text-muted-foreground">Not enriched: {o.enrichment_gaps.slice(0, 3).join("; ")}{o.enrichment_gaps.length > 3 ? "…" : ""}</p>
      )}
      {o.final_action === "WATCH" && onBuyAnyway && (
        <Button size="sm" variant="outline" onClick={() => onBuyAnyway(o)}>Buy anyway</Button>
      )}
    </CardContent></Card>
  );
}

export function Opportunities() {
  const res = useApi<Opportunity[]>("/opportunities?limit=100", { refreshOn: ["DECISION_RECORDED"] });
  const [filter, setFilter] = useState<Action | "ALL">("ALL");
  const [category, setCategory] = useState<Category>("ALL");
  const [target, setTarget] = useState<Opportunity | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const rows = useMemo(() => {
    if (!res.data) return [];
    const filtered = res.data.filter((o) => (filter === "ALL" || o.final_action === filter) && matchesCategory(o, category));
    if (category === "GAINERS") return [...filtered].sort((a, b) => (b.price_change_5m ?? 0) - (a.price_change_5m ?? 0));
    if (category === "LOSERS") return [...filtered].sort((a, b) => (a.price_change_5m ?? 0) - (b.price_change_5m ?? 0));
    return filtered;
  }, [res.data, filter, category]);
  if (res.loading && !res.data) return <Loading />;
  if (!res.data) return res.error ? <ErrorState error={res.error} onRetry={() => void res.reload()} /> : null;
  async function confirmBuy() {
    if (!target) return;
    setBusy(true); setError(null);
    try {
      const res2 = await api<{ note: string }>(`/decisions/${target.decision_id}/buy-anyway`, { method: "POST", body: {} });
      setTarget(null);
      setNotice(res2.note);
      await res.reload();
    } catch (e) { setError(toApiError(e)); } finally { setBusy(false); }
  }
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2" role="group" aria-label="Filter by action">
        {FILTERS.map((f) => <Button key={f} size="sm" variant={filter === f ? "default" : "outline"} aria-pressed={filter === f} onClick={() => setFilter(f)}>{f}</Button>)}
      </div>
      <div className="flex flex-wrap gap-2" role="group" aria-label="Filter by category">
        {CATEGORIES.map((c) => (
          <Button key={c.key} size="sm" variant={category === c.key ? "default" : "outline"} aria-pressed={category === c.key}
            title={c.title} onClick={() => setCategory(c.key)}>{c.label}</Button>
        ))}
      </div>
      {error && <ErrorState error={error} />}
      {notice && <Alert variant="success">{notice}</Alert>}
      {rows.length === 0 ? <Empty>No opportunities match. Start the agent from the Agent page; tokens appear here as they are evaluated.</Empty> : rows.map((o) => <OpportunityCard key={o.decision_id} o={o} onBuyAnyway={setTarget} />)}
      <ConfirmDialog open={target !== null} onOpenChange={(op) => { if (!op) setTarget(null); }} busy={busy} destructive
        title="Buy this token anyway?"
        description="Your strategy/AI did not qualify this token — this skips that opinion, but your Local Runner still re-checks live risk limits (liquidity, contract safety, exposure) right before buying, using fresh market data. It can still refuse the trade."
        confirmLabel="Buy anyway" onConfirm={confirmBuy} />
    </div>
  );
}
