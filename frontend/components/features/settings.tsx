"use client";
import { useState } from "react";
import Link from "next/link";
import { EmergencyControl } from "@/components/features/agent-control";
import { RiskLimitsForm } from "@/components/features/risk-limits-form";
import { ModeBadge } from "@/components/badges";
import { ErrorState, Loading } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useApi } from "@/hooks/use-api";
import { api, toApiError, type ApiError } from "@/lib/api";
import type { AgentStatus, ChainInfo, LaunchpadInfo } from "@/types/api";

interface ServerSettings { mode: "PAPER" | "LIVE"; live_trading_enabled_on_server: boolean; ai: { provider: string | null; model: string | null; note: string }; market_data: string; notifications: { implemented: boolean } }
interface Blacklist { tokens: string[]; creators: string[]; launchpads: string[] }
const KINDS = [["token", "tokens"], ["creator", "creators"], ["launchpad", "launchpads"]] as const;

function BlacklistCard() {
  const bl = useApi<Blacklist>("/controls/blacklist");
  const [kind, setKind] = useState<"token" | "creator" | "launchpad">("token");
  const [value, setValue] = useState("");
  const [error, setError] = useState<ApiError | null>(null);
  async function call(method: "POST" | "DELETE", k: string, v: string) {
    setError(null);
    try { await api("/controls/blacklist", { method, body: { kind: k, value: v } }); setValue(""); await bl.reload(); } catch (e) { setError(toApiError(e)); }
  }
  return (
    <Card><CardHeader><CardTitle>Blacklists</CardTitle></CardHeader><CardContent className="space-y-3">
      <div className="flex flex-wrap gap-2"><select aria-label="Blacklist kind" className="h-10 rounded-md border bg-background px-2 text-sm" value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
        {KINDS.map(([k]) => <option key={k} value={k}>{k}</option>)}</select>
        <Input aria-label="Blacklist value" className="min-w-0 flex-1" placeholder="address or launchpad name" value={value} onChange={(e) => setValue(e.target.value)} />
        <Button disabled={!value.trim()} onClick={() => void call("POST", kind, value.trim())}>Add</Button></div>
      {error && <ErrorState error={error} />}
      {KINDS.map(([k, key]) => (<div key={k}><p className="text-xs font-semibold">{k}s</p><div className="flex flex-wrap gap-1">
        {(bl.data?.[key] ?? []).map((v) => <Badge key={v} variant="destructive">{v.slice(0, 14)} <button aria-label={`remove ${v}`} className="ml-1" onClick={() => void call("DELETE", k, v)}>×</button></Badge>)}
        {(bl.data?.[key] ?? []).length === 0 && <span className="text-xs text-muted-foreground">none</span>}</div></div>))}
    </CardContent></Card>
  );
}

export function SettingsView() {
  const s = useApi<ServerSettings>("/settings");
  const agent = useApi<AgentStatus>("/agent", { refreshOn: ["EMERGENCY_STOP_CHANGED"] });
  const chains = useApi<ChainInfo[]>("/chains");
  const pads = useApi<{ launchpads: LaunchpadInfo[]; note: string }>("/launchpads");
  if (s.loading && !s.data) return <Loading />;
  if (!s.data) return s.error ? <ErrorState error={s.error} onRetry={() => void s.reload()} /> : null;
  const arc = chains.data?.find((c) => c.id === "arc");
  return (
    <div className="space-y-4">
      <Card><CardHeader><CardTitle>Trading mode</CardTitle></CardHeader><CardContent className="space-y-1 text-sm">
        <ModeBadge mode={s.data.mode} /><p className="text-muted-foreground">LIVE is {s.data.live_trading_enabled_on_server ? "permitted" : "disabled"} on the server and always needs explicit confirmation on the <Link className="underline" href="/agent">Agent</Link> page.</p></CardContent></Card>
      <Card><CardHeader><CardTitle>Chain</CardTitle></CardHeader><CardContent className="space-y-1 text-sm">
        <p>Arc {arc?.network ?? ""} (chain id {arc?.chain_id ?? "…"}) — the only chain enabled. BNB Chain, Solana and Robinhood Chain are extension points, not integrations.</p>
        <p>RPC: {arc?.network_status ? (arc.network_status.ok ? "reachable" : `unavailable${arc.network_status.detail ? ` (${arc.network_status.detail})` : ""}`) : "…"} · LIVE integration verified: {arc?.live_trading_verified ? "yes" : "no"}</p></CardContent></Card>
      <Card><CardHeader><CardTitle>Launchpads</CardTitle></CardHeader><CardContent className="space-y-2 text-sm">
        {(pads.data?.launchpads ?? []).map((l) => (<div key={l.id} className="flex flex-wrap items-center justify-between gap-2"><span className="font-medium">{l.name}</span>
          <span className="flex gap-1"><Badge variant={l.verified ? "success" : "warning"}>{l.verified ? "verified" : "unverified"}</Badge><Badge variant={l.enabled ? "success" : "default"}>{l.enabled ? "enabled" : "disabled"}</Badge></span></div>))}
        <p className="text-xs text-muted-foreground">{pads.data?.note}</p></CardContent></Card>
      <Card><CardHeader><CardTitle>Strategy and wallet</CardTitle></CardHeader><CardContent className="space-y-1 text-sm">
        <p>Active strategy: {agent.data ? `${agent.data.strategy.id} v${agent.data.strategy.version}` : "…"} · <Link className="underline" href="/strategies">edit weights and exits</Link></p>
        <p><Link className="underline" href="/wallet">Wallet configuration and policy</Link></p></CardContent></Card>
      <RiskLimitsForm />
      <BlacklistCard />
      <Card><CardHeader><CardTitle>AI provider</CardTitle></CardHeader><CardContent className="space-y-1 text-sm">
        <p>{s.data.ai.provider ? `${s.data.ai.provider} / ${s.data.ai.model}` : "None configured (PAPER uses deterministic entries; LIVE requires AI)"}</p><p className="text-xs text-muted-foreground">{s.data.ai.note}</p></CardContent></Card>
      <Card><CardHeader><CardTitle>Market data</CardTitle></CardHeader><CardContent className="text-sm">{s.data.market_data}</CardContent></Card>
      <Card><CardHeader><CardTitle>Notifications</CardTitle></CardHeader><CardContent className="text-sm text-muted-foreground">Not implemented yet.</CardContent></Card>
      <Card><CardHeader><CardTitle>Kill switch</CardTitle></CardHeader><CardContent className="space-y-2 text-sm">
        {agent.data ? <EmergencyControl status={agent.data} onDone={() => agent.reload()} /> : <Loading />}
        <p className="text-xs text-muted-foreground">Emergency stop blocks all new entries and survives restarts. You must disable it explicitly.</p></CardContent></Card>
    </div>
  );
}
