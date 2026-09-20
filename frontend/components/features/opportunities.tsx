"use client";
import { useState } from "react";
import Link from "next/link";
import { ActionBadge, DataLabel } from "@/components/badges";
import { Empty, ErrorState, Loading } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useApi } from "@/hooks/use-api";
import { compact, duration, num, plainPct, price, shortAddr } from "@/lib/format";
import type { Action, Opportunity } from "@/types/api";

const FILTERS: (Action | "ALL")[] = ["ALL", "BUY", "WATCH", "REJECT"];
const Cell = ({ label, value }: { label: string; value: React.ReactNode }) => (
  <div><dt className="text-[11px] text-muted-foreground">{label}</dt><dd className="text-sm tabular-nums">{value}</dd></div>
);

export function OpportunityCard({ o }: { o: Opportunity }) {
  const creator = o.creator_known ? (o.creator_sold_pct != null ? `sold ${plainPct(o.creator_sold_pct)}` : "known") : "unverified";
  return (
    <Card><CardContent className="space-y-2 pt-4">
      <div className="flex flex-wrap items-center gap-2">
        <Link className="font-semibold underline" href={`/tokens/${encodeURIComponent(o.token_key)}`}>{o.symbol ?? shortAddr(o.token_key)}</Link>
        <span className="text-xs text-muted-foreground">{o.chain}{o.launchpad ? ` · ${o.launchpad}` : ""}</span>
        <ActionBadge action={o.final_action} /><DataLabel label={o.data_label} />
        <Link className="ml-auto text-xs underline" href={`/decisions/${o.decision_id}`}>why?</Link>
      </div>
      <dl className="grid grid-cols-3 gap-2 md:grid-cols-6 lg:grid-cols-7">
        <Cell label="Age" value={duration(o.age_seconds)} /><Cell label="Price" value={price(o.price)} /><Cell label="Market cap" value={compact(o.market_cap)} />
        <Cell label="Liquidity" value={compact(o.liquidity)} /><Cell label="Volume 5m" value={compact(o.volume_5m)} /><Cell label="Buyers 5m" value={o.unique_buyers_5m ?? "—"} />
        <Cell label="Buy/sell" value={num(o.buy_sell_ratio)} /><Cell label="Holder growth" value={plainPct(o.holder_growth_pct)} /><Cell label="Top-10 holders" value={plainPct(o.top10_holder_pct)} />
        <Cell label="Creator" value={creator} /><Cell label="Strategy score" value={num(o.strategy_score, 1)} /><Cell label="Risk score" value={num(o.risk_score, 0)} />
        <Cell label="AI" value={o.ai_status} />
      </dl>
      <p className="text-xs text-muted-foreground">{o.final_reason}</p>
    </CardContent></Card>
  );
}

export function Opportunities() {
  const res = useApi<Opportunity[]>("/opportunities?limit=100", { refreshOn: ["DECISION_RECORDED"] });
  const [filter, setFilter] = useState<Action | "ALL">("ALL");
  if (res.loading && !res.data) return <Loading />;
  if (!res.data) return res.error ? <ErrorState error={res.error} onRetry={() => void res.reload()} /> : null;
  const rows = res.data.filter((o) => filter === "ALL" || o.final_action === filter);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2" role="group" aria-label="Filter">
        {FILTERS.map((f) => <Button key={f} size="sm" variant={filter === f ? "default" : "outline"} aria-pressed={filter === f} onClick={() => setFilter(f)}>{f}</Button>)}
      </div>
      {rows.length === 0 ? <Empty>No opportunities match. Start the agent from the Agent page; tokens appear here as they are evaluated.</Empty> : rows.map((o) => <OpportunityCard key={o.decision_id} o={o} />)}
    </div>
  );
}
