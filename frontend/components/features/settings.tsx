"use client";
import { useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { EmergencyControl } from "@/components/features/agent-control";
import { NotificationSettings } from "@/components/features/notification-settings";
import { RiskLimitsForm } from "@/components/features/risk-limits-form";
import { CloudRunnerPanel, RunnerModeCard } from "@/components/features/runner-mode";
import { RunnerPanel } from "@/components/features/runner-panel";
import { StrategySettings } from "@/components/features/strategy-settings";
import { WalletPolicySettings } from "@/components/features/wallet-panel";
import { ModeBadge } from "@/components/badges";
import { ErrorState, Loading } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useApi } from "@/hooks/use-api";
import { api, toApiError, type ApiError } from "@/lib/api";
import { SETTINGS_TABS, parseTab, type SettingsTab } from "@/lib/settings-tabs";
import { cn } from "@/lib/utils";
import type { AgentStatus, ChainInfo, LaunchpadInfo } from "@/types/api";

interface ServerSettings { mode: "PAPER" | "LIVE"; live_trading_enabled_on_server: boolean; ai: { provider: string | null; model: string | null; note: string }; market_data: string }
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
      <p className="text-sm text-muted-foreground">Tokens, creators or launchpads the agent must never trade.</p>
      <div className="flex flex-wrap gap-2"><select aria-label="Blacklist kind" className="h-11 rounded-md border bg-background px-2.5 text-sm" value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
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

function General({ s }: { s: ServerSettings }) {
  const chains = useApi<ChainInfo[]>("/chains");
  const pads = useApi<{ launchpads: LaunchpadInfo[]; note: string }>("/launchpads");
  const arc = chains.data?.find((c) => c.id === "arc");
  return (
    <div className="space-y-6">
      <Card><CardHeader><CardTitle>Trading mode</CardTitle></CardHeader><CardContent className="space-y-1 text-sm">
        <ModeBadge mode={s.mode} /><p className="text-muted-foreground">LIVE is {s.live_trading_enabled_on_server ? "permitted" : "disabled"} on the server and always needs explicit confirmation on the <Link className="underline" href="/agent">Agent</Link> page.</p></CardContent></Card>
      <Card><CardHeader><CardTitle>Chain</CardTitle></CardHeader><CardContent className="space-y-1 text-sm">
        <p>Arc {arc?.network ?? ""} (chain id {arc?.chain_id ?? "…"}) is the only chain enabled. BNB Chain, Solana and Robinhood Chain are extension points, not integrations.</p>
        <p>RPC: {arc?.network_status ? (arc.network_status.ok ? "reachable" : `unavailable${arc.network_status.detail ? ` (${arc.network_status.detail})` : ""}`) : "…"} · LIVE integration verified: {arc?.live_trading_verified ? "yes" : "no"}</p></CardContent></Card>
      <Card><CardHeader><CardTitle>Launchpads</CardTitle></CardHeader><CardContent className="space-y-2 text-sm">
        {(pads.data?.launchpads ?? []).map((l) => (<div key={l.id} className="flex flex-wrap items-center justify-between gap-2"><span className="font-medium">{l.name}</span>
          <span className="flex gap-1"><Badge variant={l.verified ? "success" : "warning"}>{l.verified ? "verified" : "unverified"}</Badge><Badge variant={l.enabled ? "success" : "default"}>{l.enabled ? "enabled" : "disabled"}</Badge></span></div>))}
        <p className="text-xs text-muted-foreground">{pads.data?.note}</p></CardContent></Card>
      <Card><CardHeader><CardTitle>Market data</CardTitle></CardHeader><CardContent className="text-sm">{s.market_data}</CardContent></Card>
    </div>
  );
}

function RunnerAndAi({ status, reload }: { status: AgentStatus; reload: () => Promise<void> }) {
  const chain = status.ai.chain ?? [];
  return (
    <div className="space-y-6">
      <RunnerModeCard status={status} onChanged={reload} />
      {status.execution_mode === "cloud_managed" ? <CloudRunnerPanel status={status} /> : <RunnerPanel />}
      <Card><CardHeader><CardTitle>AI providers</CardTitle></CardHeader><CardContent className="space-y-2 text-sm">
        {status.ai.mode === "ENABLED" && chain.length > 0 ? (<>
          <p>The AI is asked in this order; if a provider fails, it is retried once and the next one takes over:</p>
          <ol className="list-decimal space-y-0.5 pl-5">{chain.map((c) => <li key={c.provider}><strong>{c.provider}</strong> <span className="text-muted-foreground">{c.model}</span></li>)}</ol>
        </>) : <p className="text-muted-foreground">No AI is configured on the runner. PAPER can still trade on strategy and risk rules; LIVE requires an AI.</p>}
        <p className="text-xs text-muted-foreground">Providers and API keys are set on the runner itself (never in the browser), for example <code>ARC_RUNNER_LLM_PROVIDERS=cerebras,groq,gemini,serv</code> with each provider&apos;s <code>..._API_KEY</code>. Changes apply when the runner restarts.</p>
      </CardContent></Card>
    </div>
  );
}

export function SettingsView() {
  const router = useRouter();
  const path = usePathname();
  const params = useSearchParams();
  const tab = parseTab(params.get("tab"));
  const s = useApi<ServerSettings>("/settings");
  const agent = useApi<AgentStatus>("/agent", { refreshOn: ["EMERGENCY_STOP_CHANGED"], intervalMs: 10000 });
  const go = (t: SettingsTab) => router.replace(`${path}?tab=${t}`, { scroll: false });

  if (s.loading && !s.data) return <Loading />;
  if (!s.data) return s.error ? <ErrorState error={s.error} onRetry={() => void s.reload()} /> : null;
  const current = SETTINGS_TABS.find((t) => t.id === tab)!;

  return (
    <div className="md:flex md:gap-10">
      <nav aria-label="Settings sections" className="-mx-1 mb-6 flex gap-1 overflow-x-auto border-b px-1 pb-2 md:sticky md:top-24 md:mx-0 md:mb-0 md:w-56 md:shrink-0 md:flex-col md:self-start md:overflow-visible md:border-b-0 md:border-r md:px-0 md:pb-0 md:pr-4">
        {SETTINGS_TABS.map((t) => (
          <button key={t.id} type="button" aria-current={tab === t.id ? "page" : undefined} onClick={() => go(t.id)}
            className={cn("min-h-[44px] shrink-0 touch-manipulation rounded-md px-3.5 py-2 text-left text-sm font-medium tracking-[0.03em] transition-colors duration-200 md:min-h-[40px] md:rounded-l-none md:border-l-2", tab === t.id ? "border-accent bg-muted text-foreground max-md:shadow-[inset_0_-2px_0_hsl(var(--accent))] md:border-accent" : "text-muted-foreground hover:bg-muted/70 hover:text-foreground md:border-transparent")}>
            {t.label}
          </button>))}
      </nav>
      <div className="min-w-0 flex-1 space-y-6">
        <div><h2 className="font-serif text-2xl font-normal tracking-[-0.01em]">{current.label}</h2><p className="mt-1 text-sm text-muted-foreground">{current.hint}</p></div>
        {tab === "general" && <General s={s.data} />}
        {tab === "strategy" && <StrategySettings />}
        {tab === "risk" && <div className="space-y-6"><RiskLimitsForm /><BlacklistCard /></div>}
        {tab === "wallet" && <WalletPolicySettings />}
        {tab === "runner" && (agent.data ? <RunnerAndAi status={agent.data} reload={async () => { await agent.reload(); }} /> : agent.error ? <ErrorState error={agent.error} onRetry={() => void agent.reload()} /> : <Loading />)}
        {tab === "notifications" && <NotificationSettings />}
        {tab === "safety" && (
          <Card><CardHeader><CardTitle>Kill switch</CardTitle></CardHeader><CardContent className="space-y-2 text-sm">
            {agent.data ? <EmergencyControl status={agent.data} onDone={() => agent.reload()} /> : <Loading />}
            <p className="text-xs text-muted-foreground">Emergency stop blocks all new entries and survives restarts. You must disable it explicitly. Open positions stay protected by their stops.</p></CardContent></Card>)}
      </div>
    </div>
  );
}
