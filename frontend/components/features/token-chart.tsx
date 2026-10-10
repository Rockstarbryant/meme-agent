"use client";
import { useState } from "react";
import { CandleChart } from "@/components/charts/candle-chart";
import { PriceChart } from "@/components/price-chart";
import { Loading } from "@/components/states";
import { useApi } from "@/hooks/use-api";
import type { CandleResponse, Timeframe } from "@/types/api";

const TFS: Timeframe[] = ["1m", "5m", "15m", "1h", "4h", "1d"];
const PROVIDER_LABEL: Record<string, string> = { codex: "Codex", geckoterminal: "GeckoTerminal", snapshots: "agent snapshots (coarse)" };

/** Token price chart: candlesticks + volume + time axis, timeframe tabs, and the provider that produced the candles. */
export function TokenChart({ tokenKey, fallbackPoints, entryPrice = null }: {
  tokenKey: string; fallbackPoints: { at: string; price: number | null }[]; entryPrice?: number | null;
}) {
  const [tf, setTf] = useState<Timeframe>("15m");
  const [chain, ...rest] = tokenKey.split(":");
  const address = rest.join(":");
  const res = useApi<CandleResponse>(address ? `/discovery/tokens/${chain}/${address}/candles?tf=${tf}&limit=200` : null, { intervalMs: 30_000 });
  const d = res.data;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div role="tablist" aria-label="Timeframe" className="flex gap-1">
          {TFS.map((t) => (
            <button key={t} role="tab" aria-selected={tf === t} onClick={() => setTf(t)}
              className={`min-h-[32px] rounded-md px-2.5 text-xs font-medium tracking-[0.04em] transition-colors ${tf === t ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted/60"}`}>{t}</button>))}
        </div>
        {d && <span className="text-xs text-muted-foreground">Source: {PROVIDER_LABEL[d.provider] ?? d.provider}{d.failed.length > 0 ? ` · fell back from ${d.failed.map((f) => f.provider).join(", ")}` : ""}</span>}
      </div>
      {res.loading && !d ? <Loading label="Loading candles…" /> : d && d.candles.length >= 2
        ? <CandleChart candles={d.candles} entryPrice={entryPrice} showVolume={d.has_volume} />
        : (<div><p className="text-xs text-muted-foreground">No candles from any chart provider{d?.failed.length ? ` (${d.failed.map((f) => `${f.provider}: ${f.error}`).join("; ")})` : ""}. Showing the agent&apos;s own price samples.</p>
            <PriceChart points={fallbackPoints} /></div>)}
    </div>
  );
}
