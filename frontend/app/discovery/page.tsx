"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { AppShell } from "@/components/app-shell";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { Loading, Empty } from "@/components/states";

type TokenCard = {
  chain: string;
  token_address: string;
  symbol?: string | null;
  name?: string | null;
  launchpad?: string | null;
  market_cap?: number | null;
  liquidity?: number | null;
  holders?: number | null;
  score?: number | null;
  score_delta?: number | null;
  status?: string | null;
  priority?: string | null;
  price?: number | null;
  global_screening_passed?: boolean;
  last_monitored_at?: string | null;
};

type HistResp = { window: string; tokens: TokenCard[]; count: number };
type Bookmark = {
  id: string;
  chain: string;
  token_address: string;
  note?: string | null;
  token?: TokenCard | null;
};

function statusVariant(passed?: boolean): "success" | "default" {
  return passed ? "success" : "default";
}

function priorityVariant(p?: string | null): "warning" | "success" | "default" {
  if (p === "HOT") return "warning";
  if (p === "WARM") return "success";
  return "default";
}

function fmtScore(n?: number | null): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toFixed(1);
}

function fmtDelta(n?: number | null): string {
  if (n == null || Number.isNaN(n)) return "—";
  const sign = n > 0 ? "+" : "";
  return sign + n.toFixed(1);
}

function fmtPrice(n?: number | null): string {
  if (n == null || Number.isNaN(n)) return "—";
  if (n === 0) return "0";
  if (n < 0.0001) return n.toExponential(2);
  return n.toPrecision(4);
}

function fmtUsd(n?: number | null): string {
  if (n == null || Number.isNaN(n)) return "—";
  return "$" + Math.round(n).toLocaleString();
}

function fmtHolders(n?: number | null): string {
  if (n == null || Number.isNaN(n)) return "—";
  return String(n);
}

function shortAddr(a: string): string {
  if (!a || a.length < 12) return a || "—";
  return a.slice(0, 10) + "…";
}

export default function DiscoveryPage() {
  const [window, setWindow] = useState<"24h" | "72h">("24h");
  const [data, setData] = useState<HistResp | null>(null);
  const [q, setQ] = useState("");
  const [searchResults, setSearchResults] = useState<TokenCard[]>([]);
  const [status, setStatus] = useState<Record<string, unknown> | null>(null);
  const [bookmarks, setBookmarks] = useState<Bookmark[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // Loading is derived: the data on screen is for a different window than the one selected (or there is none yet).
  const loading = data === null || data.window !== window;

  useEffect(() => {
    api<HistResp>("/discovery/historical/" + window)
      .then(function (r) {
        setData(r);
      })
      .catch(function () {
        setData({ window: window, tokens: [], count: 0 });
      });
  }, [window]);

  useEffect(() => {
    api("/discovery/status")
      .then(function (r) {
        setStatus(r as Record<string, unknown>);
      })
      .catch(function () {});
    api<Bookmark[]>("/discovery/bookmarks")
      .then(function (r) {
        setBookmarks(Array.isArray(r) ? r : []);
      })
      .catch(function () {
        setBookmarks([]);
      });
  }, []);

  function doSearch() {
    if (!q.trim()) return;
    api<{ results: TokenCard[] }>("/discovery/search?q=" + encodeURIComponent(q))
      .then(function (r) {
        setSearchResults(r.results || []);
      })
      .catch(function () {
        setSearchResults([]);
      });
  }

  async function bookmark(t: TokenCard) {
    try {
      await api("/discovery/bookmarks", {
        method: "POST",
        body: { chain: t.chain, token_address: t.token_address },
      });
      const bms = await api<Bookmark[]>("/discovery/bookmarks");
      setBookmarks(Array.isArray(bms) ? bms : []);
    } catch {
      /* ignore dup */
    }
  }

  async function unbookmark(chain: string, address: string) {
    await api("/discovery/bookmarks/" + chain + "/" + address, { method: "DELETE" });
    setBookmarks(function (prev) {
      return prev.filter(function (b) {
        return !(b.chain === chain && b.token_address === address);
      });
    });
  }

  async function openDetail(t: TokenCard) {
    const key = t.chain + ":" + t.token_address;
    if (expanded === key) {
      setExpanded(null);
      setDetail(null);
      return;
    }
    setExpanded(key);
    setDetailLoading(true);
    setDetail(null);
    try {
      const d = await api<Record<string, unknown>>(
        "/discovery/tokens/" + t.chain + "/" + t.token_address
      );
      setDetail(d);
    } catch {
      setDetail({ token: t, history: [], error: "Could not load detail" });
    } finally {
      setDetailLoading(false);
    }
  }

  function TokenRow(props: { t: TokenCard; showBookmark?: boolean }) {
    const t = props.t;
    const showBookmark = props.showBookmark !== false;
    const key = t.chain + ":" + t.token_address;
    const isOpen = expanded === key;
    const title = t.name || t.symbol || shortAddr(t.token_address);
    const tokenHref = "/tokens/" + encodeURIComponent(key);
    const subtitle = (t.launchpad || "unknown") + " · " + shortAddr(t.token_address);

    return (
      <Card key={key}>
        <CardHeader className="pb-1">
          <button
            type="button"
            className="w-full text-left"
            onClick={function () {
              void openDetail(t);
            }}
          >
            <div className="flex items-start justify-between gap-2">
              <CardTitle className="text-base hover:underline">{title}{t.name && t.symbol ? <span className="ml-2 text-xs font-normal text-muted-foreground">{t.symbol}</span> : null}</CardTitle>
              <div className="flex gap-1">
                <Badge variant={statusVariant(t.global_screening_passed)}>
                  {t.status || "—"}
                </Badge>
                {t.priority ? (
                  <Badge variant={priorityVariant(t.priority)}>{t.priority}</Badge>
                ) : null}
              </div>
            </div>
            <p className="text-xs text-muted-foreground">{subtitle}</p>
          </button>
        </CardHeader>
        <CardContent className="space-y-2">
          <div className="grid grid-cols-2 gap-1 text-xs">
            <span>Score: {fmtScore(t.score)}</span>
            <span>Δ: {fmtDelta(t.score_delta)}</span>
            <span>Price: {fmtPrice(t.price)}</span>
            <span>Holders: {fmtHolders(t.holders)}</span>
            <span>MCap: {fmtUsd(t.market_cap)}</span>
            <span>Liq: {fmtUsd(t.liquidity)}</span>
          </div>
          {t.last_monitored_at ? (
            <p className="text-[10px] text-muted-foreground">
              Last monitored: {new Date(t.last_monitored_at).toLocaleString()}
            </p>
          ) : null}

          {isOpen ? (
            <div className="rounded-md border p-2 text-xs space-y-1">
              {detailLoading ? (
                <Loading label="Loading detail…" />
              ) : detail ? (
                <>
                  <p className="font-medium break-all">
                    {t.chain}:{t.token_address}
                  </p>
                  <p className="text-muted-foreground">
                    Metrics refresh via monitoring. Thin data → low score → REJECTED/COLD is normal until holders and buy/sell enrich.
                  </p>
                  <p>
                    <Link href={tokenHref} className="text-primary underline">
                      Open the full token page
                    </Link>
                    {" · "}
                    <Link href="/opportunities" className="text-primary underline">
                      Opportunities
                    </Link>
                    {" "}
                    (Buy anyway appears there once the agent records a WATCH decision).
                  </p>
                </>
              ) : null}
            </div>
          ) : null}

          <div className="flex gap-2">
            <Link href={tokenHref} className="flex-1">
              <Button size="sm" className="w-full">
                Token page
              </Button>
            </Link>
            <Button
              size="sm"
              variant="outline"
              className="flex-1"
              onClick={function () {
                void openDetail(t);
              }}
            >
              {isOpen ? "Hide quick view" : "Quick view"}
            </Button>
            {showBookmark ? (
              <Button
                size="sm"
                variant="outline"
                className="flex-1"
                onClick={function () {
                  void bookmark(t);
                }}
              >
                Bookmark
              </Button>
            ) : null}
          </div>
        </CardContent>
      </Card>
    );
  }

  const premiumOn = Boolean(status && status.premium_scanner_enabled);
  const intervalH = (status && status.discovery_interval_hours) || 3;
  const statusMsg = status && typeof status.message === "string" ? status.message : null;

  return (
    <AppShell>
      <div className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h1 className="text-xl font-bold">Token Discovery</h1>
          {status ? (
            <Badge variant={premiumOn ? "success" : "default"}>
              {premiumOn ? "Premium on" : "Premium off"} · global · every {String(intervalH)}h
            </Badge>
          ) : null}
        </div>
        <p className="text-sm text-muted-foreground">
          Historical tokens from the global registry. Market data is refreshed by monitoring.
          Opening this page does not trigger a launchpad scan. For Buy anyway, use Opportunities after the agent records a WATCH decision.
        </p>
        {statusMsg ? (
          <p className="text-xs text-muted-foreground">{statusMsg}</p>
        ) : null}

        <div className="flex gap-2">
          <Button
            size="sm"
            variant={window === "24h" ? "default" : "outline"}
            onClick={function () {
              setWindow("24h");
            }}
          >
            Last 24 Hours
          </Button>
          <Button
            size="sm"
            variant={window === "72h" ? "default" : "outline"}
            onClick={function () {
              setWindow("72h");
            }}
          >
            Last 72 Hours
          </Button>
        </div>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Search tokens</CardTitle>
          </CardHeader>
          <CardContent className="flex gap-2">
            <Input
              placeholder="name, symbol, address, launchpad…"
              value={q}
              onChange={function (e) {
                setQ(e.target.value);
              }}
              onKeyDown={function (e) {
                if (e.key === "Enter") doSearch();
              }}
            />
            <Button onClick={doSearch}>Search</Button>
          </CardContent>
        </Card>

        {searchResults.length > 0 ? (
          <div className="grid gap-3 sm:grid-cols-2">
            {searchResults.map(function (t) {
              return <TokenRow key={"s-" + t.token_address} t={t} />;
            })}
          </div>
        ) : null}

        {bookmarks.length > 0 ? (
          <div className="space-y-2">
            <h2 className="text-lg font-semibold">Bookmarks</h2>
            <div className="grid gap-3 sm:grid-cols-2">
              {bookmarks.map(function (b) {
                return (
                  <div key={b.id} className="space-y-1">
                    {b.token ? (
                      <TokenRow t={b.token} showBookmark={false} />
                    ) : (
                      <Card>
                        <CardContent className="p-3 text-sm">
                          {b.chain}:{b.token_address}
                        </CardContent>
                      </Card>
                    )}
                    <Button
                      size="sm"
                      variant="ghost"
                      className="w-full"
                      onClick={function () {
                        void unbookmark(b.chain, b.token_address);
                      }}
                    >
                      Remove bookmark
                    </Button>
                  </div>
                );
              })}
            </div>
          </div>
        ) : null}

        <h2 className="text-lg font-semibold">Discovered · {window}</h2>
        {loading ? (
          <Loading label="Loading historical tokens…" />
        ) : !data || data.count === 0 ? (
          <Empty>
            No tokens in this window yet. Global discovery has not populated candidates for this period.
          </Empty>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            {data.tokens.map(function (t) {
              return (
                <TokenRow key={t.chain + ":" + t.token_address} t={t} />
              );
            })}
          </div>
        )}
      </div>
    </AppShell>
  );
}