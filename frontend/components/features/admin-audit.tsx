"use client";
import Link from "next/link";
import { useState } from "react";
import { Empty, ErrorState, Loading } from "@/components/states";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useApi } from "@/hooks/use-api";
import { useAuth } from "@/lib/auth";
import { ago } from "@/lib/format";
import type { AuditKind, AuditList, AuditRow, ProviderBoardRow } from "@/types/api";

const KINDS: { v: "" | AuditKind; l: string }[] = [
  { v: "", l: "All kinds" }, { v: "MARKET_PROVIDER", l: "Market data" }, { v: "ENRICHMENT_PROVIDER", l: "Enrichment" },
  { v: "AI_PROVIDER", l: "AI provider" }, { v: "AI_AGENT", l: "AI agent steps" }, { v: "VENUE_QUOTE", l: "Venue quotes" },
  { v: "EXECUTION", l: "Execution" }, { v: "CHART_PROVIDER", l: "Charts" },
];
const STATUSES = ["", "FAILED", "SKIPPED", "DEGRADED", "RECOVERED", "OK"];
const WINDOWS: { h: number; l: string }[] = [{ h: 1, l: "1h" }, { h: 24, l: "24h" }, { h: 72, l: "3d" }, { h: 168, l: "7d" }];
const STATE_VARIANT = { DOWN: "destructive", DEGRADED: "warning", OK: "success" } as const;
const STATUS_VARIANT = { OK: "success", RECOVERED: "success", FAILED: "destructive", SKIPPED: "warning", DEGRADED: "warning" } as const;
const KIND_LABEL: Record<string, string> = Object.fromEntries(KINDS.filter((k) => k.v).map((k) => [k.v, k.l]));

function Select({ value, onChange, options, label }: { value: string; onChange: (v: string) => void; options: { v: string; l: string }[]; label: string }) {
  return (
    <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}
      className="h-10 rounded-md border bg-background px-2 text-sm">{options.map((o) => <option key={o.v} value={o.v}>{o.l}</option>)}</select>
  );
}

export function AdminAudit() {
  const { user } = useAuth();
  const [kind, setKind] = useState(""), [status, setStatus] = useState(""), [provider, setProvider] = useState("");
  const [token, setToken] = useState(""), [q, setQ] = useState(""), [hours, setHours] = useState(24), [limit, setLimit] = useState(100);
  const qs = new URLSearchParams({ limit: String(limit), hours: String(hours) });
  if (kind) qs.set("kind", kind);
  if (status) qs.set("status", status);
  if (provider.trim()) qs.set("provider", provider.trim().toLowerCase());
  if (token.trim()) qs.set("token", token.trim().toLowerCase());
  if (q.trim()) qs.set("q", q.trim());
  const board = useApi<{ providers: ProviderBoardRow[] }>(`/admin/audit/providers?window_h=${hours}`, { intervalMs: 15_000 });
  const list = useApi<AuditList>(`/admin/audit?${qs.toString()}`, { intervalMs: 15_000 });

  if (!user?.is_admin) return <Empty>This page is for administrators only.</Empty>;
  const down = (board.data?.providers ?? []).filter((p) => p.state === "DOWN");
  return (
    <div className="mt-6 space-y-6">
      {down.length > 0 && <Alert variant="destructive"><p className="font-medium">Currently failing: {down.map((p) => `${p.provider} (${KIND_LABEL[p.kind] ?? p.kind})`).join(", ")}</p>
        <p className="mt-1 text-xs">Their latest recorded event is a failure. Fallback providers are answering where configured.</p></Alert>}
      <Card><CardHeader><CardTitle>Provider health</CardTitle></CardHeader><CardContent>
        {board.loading && !board.data ? <Loading /> : board.error ? <ErrorState error={board.error} onRetry={() => void board.reload()} />
          : (board.data?.providers.length ?? 0) === 0 ? <Empty>No provider activity recorded in this window.</Empty>
          : <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{board.data!.providers.map((p) => (
              <button key={`${p.kind}:${p.provider}`} onClick={() => { setProvider(p.provider); setKind(p.kind); }} className="rounded-lg border p-3 text-left transition-colors hover:bg-muted/50">
                <div className="flex items-center justify-between gap-2"><span className="font-medium">{p.provider}</span><Badge variant={STATE_VARIANT[p.state]}>{p.state}</Badge></div>
                <p className="mt-0.5 text-xs text-muted-foreground">{KIND_LABEL[p.kind] ?? p.kind}</p>
                <p className="mt-2 text-xs">ok {p.ok} · failed {p.failed} · skipped {p.skipped}{p.avg_latency_ms != null ? ` · ~${Math.round(p.avg_latency_ms)} ms` : ""}</p>
                <p className="text-xs text-muted-foreground">last ok {ago(p.last_ok_at)} · last failure {ago(p.last_failure_at)}</p>
              </button>))}</div>}
      </CardContent></Card>

      <Card><CardHeader><CardTitle>Events</CardTitle></CardHeader><CardContent className="space-y-4">
        <div className="flex flex-wrap gap-2">
          <Select label="Kind" value={kind} onChange={setKind} options={KINDS.map((k) => ({ v: k.v, l: k.l }))} />
          <Select label="Status" value={status} onChange={setStatus} options={STATUSES.map((s) => ({ v: s, l: s || "Any status" }))} />
          <Select label="Window" value={String(hours)} onChange={(v) => setHours(Number(v))} options={WINDOWS.map((w) => ({ v: String(w.h), l: `Last ${w.l}` }))} />
          <Input aria-label="Provider" className="w-36" placeholder="provider" value={provider} onChange={(e) => setProvider(e.target.value)} />
          <Input aria-label="Token" className="w-48" placeholder="arc:0x… token" value={token} onChange={(e) => setToken(e.target.value)} />
          <Input aria-label="Search error text" className="w-44" placeholder="search error text" value={q} onChange={(e) => setQ(e.target.value)} />
          <Button variant="outline" size="sm" onClick={() => { setKind(""); setStatus(""); setProvider(""); setToken(""); setQ(""); setLimit(100); }}>Reset</Button>
        </div>
        {list.loading && !list.data ? <Loading /> : list.error ? <ErrorState error={list.error} onRetry={() => void list.reload()} />
          : (list.data?.items.length ?? 0) === 0 ? <Empty>No events match. Failures are always recorded; successes are sampled once a minute per provider.</Empty>
          : <ul className="divide-y">{list.data!.items.map((r) => <Row key={r.id} r={r} />)}</ul>}
        {list.data && list.data.items.length >= limit && limit < 500 && <Button variant="outline" size="sm" onClick={() => setLimit(Math.min(500, limit + 100))}>Load more</Button>}
      </CardContent></Card>
    </div>
  );
}

function Row({ r }: { r: AuditRow }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="py-2.5">
      <button className="w-full text-left" onClick={() => setOpen(!open)} aria-expanded={open}>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={STATUS_VARIANT[r.status]}>{r.status}</Badge>
          <span className="text-sm font-medium">{r.provider || "—"}{r.model ? ` / ${r.model}` : ""}</span>
          <span className="text-xs text-muted-foreground">{KIND_LABEL[r.kind] ?? r.kind} · {r.operation}</span>
          {r.repeat > 1 && <Badge>×{r.repeat}</Badge>}
          <span className="ml-auto text-xs text-muted-foreground" title={new Date(r.at).toLocaleString()}>{new Date(r.at).toLocaleTimeString()} · {ago(r.at)}</span>
        </div>
        {r.error && <p className="mt-1 break-words text-xs text-destructive">{r.error}</p>}
        <p className="mt-0.5 text-xs text-muted-foreground">{[r.token_key, r.latency_ms != null ? `${Math.round(r.latency_ms)} ms` : "", r.component].filter(Boolean).join(" · ")}</p>
      </button>
      {open && (
        <div className="mt-2 space-y-2 rounded-md bg-muted/40 p-3 text-xs">
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words">{JSON.stringify(r.detail, null, 2)}</pre>
          <p>{r.user_id ? `user ${r.user_id}` : "system / shared pipeline"}</p>
          {r.decision_id && !r.decision_id.startsWith("exit:") && <Link className="underline" href={`/decisions/${r.decision_id}`}>Open decision {r.decision_id.slice(0, 8)}</Link>}
          {r.decision_id && <p className="text-muted-foreground">Everything recorded for this decision: /admin/audit/decision/{r.decision_id}</p>}
        </div>)}
    </li>
  );
}
