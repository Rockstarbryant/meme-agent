"use client";
import { useState } from "react";
import Link from "next/link";
import { Cloud, Laptop } from "lucide-react";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { ErrorState } from "@/components/states";
import { useToast } from "@/components/toast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, toApiError, type ApiError } from "@/lib/api";
import { ago } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { AgentStatus, ExecutionMode } from "@/types/api";

interface OptionProps {
  mode: ExecutionMode; current: boolean; title: string; icon: typeof Cloud; blurb: string; lines: string[];
  badge: { label: string; variant: "success" | "warning" | "default" }; disabledReason?: string | null; onChoose: (m: ExecutionMode) => void; busy: boolean;
}
function Option({ mode, current, title, icon: Icon, blurb, lines, badge, disabledReason, onChoose, busy }: OptionProps) {
  return (
    <div className={cn("relative flex flex-col gap-3 rounded-lg border p-5 transition-colors duration-200", current ? "border-accent bg-accent/[0.06] before:absolute before:inset-x-0 before:-top-px before:h-0.5 before:rounded-t-lg before:bg-accent" : "hover:border-border-hover")} data-testid={`runner-option-${mode}`}>
      <div className="flex items-center gap-2"><Icon className="h-5 w-5 text-accent-ink" aria-hidden /><h3 className="font-serif text-lg font-semibold">{title}</h3>
        {current && <Badge variant="success">IN USE</Badge>}<Badge variant={badge.variant}>{badge.label}</Badge></div>
      <p className="text-sm text-muted-foreground">{blurb}</p>
      <ul className="list-disc space-y-1 pl-5 text-xs leading-relaxed text-muted-foreground marker:text-accent">{lines.map((l) => <li key={l}>{l}</li>)}</ul>
      <div className="mt-auto pt-1">
        {current ? <p className="text-xs text-muted-foreground">This is your active runner.</p>
          : <Button size="sm" variant="outline" disabled={busy || !!disabledReason} onClick={() => onChoose(mode)}>{`Use ${mode === "cloud_managed" ? "cloud" : "local"} runner`}</Button>}
        {!current && disabledReason && <p className="mt-1 text-xs text-warning">{disabledReason}</p>}
      </div>
    </div>
  );
}

/** Explicit choice between the managed cloud runner and the user's own machine. */
export function RunnerModeCard({ status, onChanged }: { status: AgentStatus; onChanged: () => void | Promise<void> }) {
  const { toast } = useToast();
  const [target, setTarget] = useState<ExecutionMode | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const mode: ExecutionMode = status.execution_mode ?? "self_hosted";
  const r = status.runners;
  const cloudUnavailable = r && !r.cloud.available ? "The cloud runner is not enabled on this server." : null;

  async function switchTo() {
    if (!target) return;
    setBusy(true); setError(null);
    try {
      await api("/agent/execution-mode", { method: "POST", body: { mode: target } });
      toast({ id: "runner-mode", kind: "success", title: target === "cloud_managed" ? "Switched to the cloud runner" : "Switched to your local runner",
        description: "The agent is stopped. Press Start when you are ready." });
      setTarget(null);
      await onChanged();
    } catch (e) { setError(toApiError(e)); } finally { setBusy(false); }
  }

  return (
    <Card>
      <CardHeader><CardTitle>Where does your agent run?</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">Pick one. Each runner keeps its own positions, so you can switch only while nothing is open.</p>
        <div className="grid gap-4 md:grid-cols-2">
          <Option mode="cloud_managed" current={mode === "cloud_managed"} icon={Cloud} title="Cloud runner" busy={busy} onChoose={setTarget}
            blurb="We run the agent for you, always on. Nothing to install."
            lines={["PAPER mode works immediately: no wallet needed.", "LIVE mode needs a Privy wallet (create it on the Wallet page).", "Positions are monitored 24/7."]}
            badge={r ? (r.cloud.online ? { label: "ONLINE", variant: "success" } : { label: "WAITING FOR FIRST START", variant: "default" }) : { label: "…", variant: "default" }}
            disabledReason={cloudUnavailable} />
          <Option mode="self_hosted" current={mode === "self_hosted"} icon={Laptop} title="Local runner" busy={busy} onChoose={setTarget}
            blurb="The agent runs on your own computer or server, next to your own wallet session."
            lines={["You install and keep it running (python -m runner run).", "Your keys never leave your machine.", "Nothing is monitored while your machine is off."]}
            badge={r ? (r.local.paired ? (r.local.online ? { label: "ONLINE", variant: "success" } : { label: "OFFLINE", variant: "warning" }) : { label: "NOT PAIRED", variant: "default" }) : { label: "…", variant: "default" }} />
        </div>
        {mode === "cloud_managed" && r && !r.cloud.has_wallet && status.mode === "PAPER" && (
          <p className="text-xs text-muted-foreground">You are in PAPER mode, so no wallet is needed. To trade LIVE later, create a cloud wallet on the <Link className="underline" href="/wallet">Wallet page</Link>.</p>
        )}
        {r && (r.cloud.last_seen_at || r.local.last_seen_at) && (
          <p className="text-xs text-muted-foreground">Last heard from: cloud {ago(r.cloud.last_seen_at)} · local {ago(r.local.last_seen_at)}</p>
        )}
      </CardContent>
      <ConfirmDialog open={target !== null} onOpenChange={(o) => { if (!o) { setTarget(null); setError(null); } }} busy={busy}
        title={target === "cloud_managed" ? "Switch to the cloud runner?" : "Switch to your local runner?"}
        description="Your agent will be stopped and must be started again on the new runner. Switching is refused while you have open positions, because each runner manages its own."
        confirmLabel="Switch runner" extra={error ? <ErrorState error={error} /> : null} onConfirm={switchTo} />
    </Card>
  );
}

/** Status of the managed cloud runner (what the Local Runner panel is for the other mode). */
export function CloudRunnerPanel({ status }: { status: AgentStatus }) {
  const r = status.runners?.cloud;
  const runner = status.runner;
  return (
    <Card>
      <CardHeader><CardTitle>Cloud runner</CardTitle></CardHeader>
      <CardContent className="space-y-2 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={runner?.online ? "success" : "warning"}>{runner?.online ? "ONLINE" : "NOT REPORTING"}</Badge>
          <span className="text-xs text-muted-foreground">last heard {ago(runner?.last_seen_at ?? r?.last_seen_at)}</span>
        </div>
        <p className="text-muted-foreground">Your agent runs on our servers; there is nothing to install or pair.{" "}
          {!runner?.online && "A new cloud account appears here a few seconds after you press Start."}</p>
        <p className="text-xs text-muted-foreground">Wallet: {r?.has_wallet ? <>Privy wallet <code className="break-all">{r.wallet_address}</code></> : status.mode === "LIVE" ? <Link className="underline" href="/wallet">create a cloud wallet to trade LIVE</Link> : "none needed in PAPER mode"}</p>
      </CardContent>
    </Card>
  );
}
