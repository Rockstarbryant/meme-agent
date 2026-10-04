"use client";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { compact, num, plainPct } from "@/lib/format";

/** Trading windows the backend can report (absent window = the data source could not supply it). */
export const WINDOWS = ["5m", "15m", "1h", "2h", "4h", "6h", "12h", "24h"] as const;
const GROWTH_WINDOWS = ["1h", "6h", "24h"] as const;

type Market = Record<string, unknown>;
type Win = {
  buyers?: number | null; sellers?: number | null; buys?: number | null; sells?: number | null;
  volume_usd?: number | null; buy_volume_usd?: number | null; sell_volume_usd?: number | null;
  price_change_pct?: number | null; basis?: string | null;
};

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const cnt = (v: unknown) => (isNum(v) ? String(v) : "—");
const chg = (v: unknown) => (isNum(v) ? `${v > 0 ? "+" : ""}${v.toFixed(2)}%` : "—");
const chgClass = (v: unknown) => (!isNum(v) || v === 0 ? "" : v > 0 ? "text-success" : "text-destructive");

/** Older decisions only carry the flat 5m / 15m fields; show those so the table is never empty. */
function windowOf(m: Market, w: string, wins: Record<string, Win>): Win | null {
  if (wins[w]) return wins[w];
  if (w === "5m" && (isNum(m.volume_5m) || isNum(m.unique_buyers_5m))) {
    return { buyers: m.unique_buyers_5m as number | null, sellers: m.unique_sellers_5m as number | null,
      buys: m.buys_5m as number | null, sells: m.sells_5m as number | null, volume_usd: m.volume_5m as number | null,
      buy_volume_usd: m.buy_volume_5m as number | null, sell_volume_usd: m.sell_volume_5m as number | null,
      price_change_pct: m.price_change_5m as number | null, basis: "flat fields" };
  }
  if (w === "15m" && (isNum(m.volume_15m) || isNum(m.unique_buyers_15m) || isNum(m.price_change_15m))) {
    return { buyers: m.unique_buyers_15m as number | null, sellers: m.unique_sellers_15m as number | null,
      volume_usd: m.volume_15m as number | null, price_change_pct: m.price_change_15m as number | null, basis: "flat fields" };
  }
  return null;
}

/** Buyers, sellers, volume and price change for every time window, one row per window. */
export function MarketWindowsCard({ market }: { market: Market }) {
  const wins = (market.windows ?? {}) as Record<string, Win>;
  const rows = WINDOWS.map((w) => [w, windowOf(market, w, wins)] as const);
  const anyBasis = rows.some(([, x]) => x && x.basis);
  return (
    <Card><CardHeader><CardTitle>Trading by time window</CardTitle></CardHeader><CardContent className="space-y-2">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[34rem] text-sm tabular-nums">
          <thead><tr className="text-left text-xs text-muted-foreground">
            <th className="py-1 pr-2 font-normal">Window</th><th className="pr-2 font-normal">Buyers</th><th className="pr-2 font-normal">Sellers</th>
            <th className="pr-2 font-normal">Buys / sells</th><th className="pr-2 font-normal">Volume</th>
            <th className="pr-2 font-normal">Buy vol</th><th className="pr-2 font-normal">Sell vol</th><th className="font-normal">Price Δ</th></tr></thead>
          <tbody>{rows.map(([w, x]) => (
            <tr key={w} className="border-t">
              <td className="py-1 pr-2 font-medium">{w}</td><td className="pr-2">{cnt(x?.buyers)}</td><td className="pr-2">{cnt(x?.sellers)}</td>
              <td className="pr-2">{x ? `${cnt(x.buys)} / ${cnt(x.sells)}` : "—"}</td>
              <td className="pr-2">{compact(x?.volume_usd)}</td><td className="pr-2">{compact(x?.buy_volume_usd)}</td><td className="pr-2">{compact(x?.sell_volume_usd)}</td>
              <td className={chgClass(x?.price_change_pct)}>{chg(x?.price_change_pct)}</td></tr>))}</tbody>
        </table>
      </div>
      <p className="text-xs text-muted-foreground">
        Buyers / sellers are unique trading addresses. A dash means the data sources could not supply that figure; it is not zero.
        {anyBasis ? " Windows come from the pool's own statistics, completed from its trade history where that history covers the whole window." : ""}
      </p>
    </CardContent></Card>
  );
}

/** Holder count, growth over several windows, and concentration (top 5 / 10 / 20 holders and top 5% / 20% / 30% of holders). */
export function HoldersCard({ market }: { market: Market }) {
  const growth = (market.holder_growth ?? {}) as Record<string, number>;
  const hasWindowed = GROWTH_WINDOWS.some((w) => isNum(growth[w]));
  const basis = typeof market.holder_basis === "string" ? market.holder_basis : null;
  const sampled = isNum(market.holders_sampled) ? market.holders_sampled : null;
  const cell = (label: string, v: unknown) => (<div key={label}><p className="text-xs text-muted-foreground">{label}</p><p>{isNum(v) ? plainPct(v) : "—"}</p></div>);
  return (
    <Card><CardHeader><CardTitle>Holders</CardTitle></CardHeader><CardContent className="space-y-3 text-sm">
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        <div><p className="text-xs text-muted-foreground">Holder count</p><p>{isNum(market.holder_count) ? market.holder_count.toLocaleString() : "—"}</p></div>
        {GROWTH_WINDOWS.map((w) => (<div key={w}><p className="text-xs text-muted-foreground">Growth {w}</p>
          <p className={chgClass(growth[w])}>{isNum(growth[w]) ? chg(growth[w]) : "—"}</p></div>))}
      </div>
      {!hasWindowed && <p className="text-xs text-muted-foreground">Holder growth is measured from this worker&apos;s own history and fills in after about 1h of running.</p>}
      <div className="grid grid-cols-3 gap-2 md:grid-cols-6">
        {cell("Top 5 holders", market.top5_holder_pct)}{cell("Top 10 holders", market.top10_holder_pct)}{cell("Top 20 holders", market.top20_holder_pct)}
        {cell("Top 5% of holders", market.top_5pct_holders_pct)}{cell("Top 20% of holders", market.top_20pct_holders_pct)}{cell("Top 30% of holders", market.top_30pct_holders_pct)}
      </div>
      <p className="text-xs text-muted-foreground">
        Shares of circulating supply{basis ? ` (${basis})` : ""}{sampled != null && !basis ? ` from ${num(sampled, 0)} holder rows` : ""}.
        A dash on a percentage band means too few holder rows could be fetched to compute it exactly.
      </p>
    </CardContent></Card>
  );
}
