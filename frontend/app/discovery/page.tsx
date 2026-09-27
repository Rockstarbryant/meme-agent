"use client";
import { useEffect, useState } from "react";
import { AppShell } from "@/components/app-shell";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { Loading, Empty } from "@/components/states";

type TokenCard = {
  chain: string; token_address: string; symbol?: string | null; name?: string | null;
  launchpad?: string | null; market_cap?: number | null; liquidity?: number | null;
  holders?: number | null; score?: number | null; score_delta?: number | null;
  status?: string | null; priority?: string | null; price?: number | null;
  global_screening_passed?: boolean; last_monitored_at?: string | null;
};
type HistResp = { window: string; tokens: TokenCard[]; count: number };
type Bookmark = { id: string; chain: string; token_address: string; note?: string | null; token?: TokenCard | null };

function statusVariant(passed?: boolean): "success" | "default" {
  return passed ? "success" : "default";
}

function priorityVariant(p?: string | null): "warning" | "success" | "default" {
  if (p === "HOT") return "warning";
  if (p === "WARM") return "success";
  return "default";
}

export default function DiscoveryPage() {
  const [window, setWindow] = useState<"24h" | "72h">("24h");
  const [data, setData] = useState<HistResp | null>(null);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [searchResults, setSearchResults] = useState<TokenCard[]>([]);
  const [status, setStatus] = useState<any>(null);
  const [bookmarks, setBookmarks] = useState<Bookmark[]>([]);

  useEffect(() => {
    setLoading(true);
    api<HistResp>(`/discovery/historical/${window}`)
      .then((r) => setData(r))
      .catch(() => setData({ window, tokens: [], count: 0 }))
      .finally(() => setLoading(false));
  }, [window]);

  useEffect(() => {
    api("/discovery/status").then(setStatus).catch(() => {});
    api<Bookmark[]>("/discovery/bookmarks").then((r) => setBookmarks(Array.isArray(r) ? r : [])).catch(() => setBookmarks([]));
  }, []);

  const doSearch = () => {
    if (!q.trim()) return;
    api<{ results: TokenCard[] }>(`/discovery/search?q=${encodeURIComponent(q)}`)
      .then((r) => setSearchResults(r.results || []))
      .catch(() => setSearchResults([]));
  };

  const bookmark = async (t: TokenCard) => {
    try {
      await api("/discovery/bookmarks", { method: "POST", body: { chain: t.chain, token_address: t.token_address } });
      const bms = await api<Bookmark[]>("/discovery/bookmarks");
      setBookmarks(Array.isArray(bms) ? bms : []);
    } catch { /* ignore dup */ }
  };

  const unbookmark = async (chain: string, address: string) => {
    await api(`/discovery/bookmarks/\( {chain}/ \){address}`, { method: "DELETE" });
    setBookmarks((prev) => prev.filter((b) => !(b.chain === chain && b.token_address === address)));
  };

  const TokenRow = ({ t, showBookmark = true }: { t: TokenCard; showBookmark?: boolean }) => (
    <Card key={`\( {t.chain}: \){t.token_address}`}>
      <CardHeader className="pb-1">
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="text-base">{t.symbol || `${t.token_address.slice(0, 12)}…`}</CardTitle>
          <div className="flex gap-1">
            <Badge variant={statusVariant(t.global_screening_passed)}>{t.status || "—"}</Badge>
            {t.priority ? <Badge variant={priorityVariant(t.priority)}>{t.priority}</Badge> : null}
          </div>
        </div>
        <p className="text-xs text-muted-foreground">{t.launchpad || "unknown"} · {t.token_address.slice(0, 10)}…</p>
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="grid grid-cols-2 gap-1 text-xs">
          <span>Score: {t.score != null ? t.score.toFixed(1) : "—"}</span>
          <span>Δ: {t.score_delta != null ? `\( {t.score_delta > 0 ? "+" : ""} \){t.score_delta.toFixed(1)}` : "—"}</span>
          <span>Price: {t.price != null ? t.price.toPrecision(4) : "—"}</span>
          <span>Holders: {t.holders != null ? t.holders : "—"}</span>
          <span>MCap: {t.market_cap != null ? `\[ {Math.round(t.market_cap).toLocaleString()}` : "—"}</span>
          <span>Liq: {t.liquidity != null ? ` \]{Math.round(t.liquidity).toLocaleString()}` : "—"}</span>
        </div>
        {t.last_monitored_at && (
          <p className="text-[10px] text-muted-foreground">Last monitored: {new Date(t.last_monitored_at).toLocaleString()}</p>
        )}
        {showBookmark && (
          <Button size="sm" variant="outline" className="w-full" onClick={() => void bookmark(t)}>Bookmark</Button>
        )}
      </CardContent>
    </Card>
  );

  return (
    <AppShell>
      <div className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h1 className="text-xl font-bold">Token Discovery</h1>
          {status && (
            <Badge variant={status.premium_scanner_enabled ? "success" : "default"}>
              {status.premium_scanner_enabled ? "Premium on" : "Premium off"} · global · every {status.discovery_interval_hours ?? 3}h
            </Badge>
          )}
        </div>
        <p className="text-sm text-muted-foreground">
          Historical tokens from the global registry. Market data (price, mcap, liquidity, holders) is refreshed by monitoring — not frozen at discovery.
          Opening this page does not trigger a launchpad scan.
        </p>
        {status?.message && <p className="text-xs text-muted-foreground">{status.message}</p>}

        <div className="flex gap-2">
          <Button size="sm" variant={window === "24h" ? "default" : "outline"} onClick={() => setWindow("24h")}>Last 24 Hours</Button>
          <Button size="sm" variant={window === "72h" ? "default" : "outline"} onClick={() => setWindow("72h")}>Last 72 Hours</Button>
        </div>

        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-base">Search tokens</CardTitle></CardHeader>
          <CardContent className="flex gap-2">
            <Input
              placeholder="name, symbol, address, launchpad…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && doSearch()}
            />
            <Button onClick={doSearch}>Search</Button>
          </CardContent>
        </Card>

        {searchResults.length > 0 && (
          <div className="grid gap-3 sm:grid-cols-2">
            {searchResults.map((t) => <TokenRow key={`s-${t.token_address}`} t={t} />)}
          </div>
        )}

        {bookmarks.length > 0 && (
          <div className="space-y-2">
            <h2 className="text-lg font-semibold">Bookmarks</h2>
            <div className="grid gap-3 sm:grid-cols-2">
              {bookmarks.map((b) => (
                <div key={b.id} className="space-y-1">
                  {b.token ? (
                    <TokenRow t={b.token} showBookmark={false} />
                  ) : (
                    <Card><CardContent className="p-3 text-sm">{b.chain}:{b.token_address}</CardContent></Card>
                  )}
                  <Button size="sm" variant="ghost" className="w-full" onClick={() => void unbookmark(b.chain, b.token_address)}>
                    Remove bookmark
                  </Button>
                </div>
              ))}
            </div>
          </div>
        )}

        <h2 className="text-lg font-semibold">Discovered · {window}</h2>
        {loading ? (
          <Loading label="Loading historical tokens…" />
        ) : !data || data.count === 0 ? (
          <Empty>
            No tokens in this window yet. Global discovery has not populated candidates for this period.
          </Empty>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            {data.tokens.map((t) => <TokenRow key={`\( {t.chain}: \){t.token_address}`} t={t} />)}
          </div>
        )}
      </div>
    </AppShell>
  );
}