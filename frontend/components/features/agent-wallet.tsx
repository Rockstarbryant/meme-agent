"use client";
import { useMemo, useState } from "react";
import { ArrowDownToLine, ArrowUpFromLine, Check, Copy, ExternalLink, History, Wallet as WalletIcon } from "lucide-react";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { ModeTabs, type HistoryMode } from "@/components/mode-tabs";
import { ErrorState } from "@/components/states";
import { useToast } from "@/components/toast";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { useApi } from "@/hooks/use-api";
import { ApiError, api, toApiError } from "@/lib/api";
import { waitForCommand } from "@/lib/commands";
import { ago, pnlClass, shortAddr, signedUsd, usd } from "@/lib/format";
import type { Portfolio, WalletState } from "@/types/api";

export interface LedgerEntry {
  at: string; kind: "BUY" | "SELL" | "WITHDRAWAL" | "EVENT"; title: string; amount_usdc: number | null; status: string; mode: string | null;
  simulated: boolean; tx_hash: string | null; explorer_url: string | null; detail: string | null; fee_usdc?: number | null;
}

export const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/;
/** Returns an error message, or null when the withdrawal request looks valid. */
export function validateWithdrawal(to: string, amountText: string, balance: number | null, ownAddress: string | null): string | null {
  if (!ADDRESS_RE.test(to.trim())) return "Enter a valid destination address: 0x followed by 40 hex characters.";
  if (ownAddress && to.trim().toLowerCase() === ownAddress.toLowerCase()) return "That is this wallet's own address. Enter an address you control elsewhere.";
  const amount = Number(amountText);
  if (!amountText.trim() || !Number.isFinite(amount) || amount <= 0) return "Enter an amount greater than zero.";
  if (balance != null && amount > balance + 1e-9) return `The amount is more than the wallet's balance (${usd(balance)}).`;
  return null;
}

function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <Button size="sm" variant="outline" aria-label={`${label} address`} onClick={async () => {
      try { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 2000); } catch { /* clipboard blocked */ }
    }}>{done ? <><Check className="mr-1 h-3.5 w-3.5" aria-hidden />Copied</> : <><Copy className="mr-1 h-3.5 w-3.5" aria-hidden />{label}</>}</Button>
  );
}

const KIND_STYLE: Record<LedgerEntry["kind"], { label: string; variant: "success" | "warning" | "default" | "destructive" }> = {
  BUY: { label: "Buy", variant: "default" }, SELL: { label: "Sell", variant: "success" }, WITHDRAWAL: { label: "Withdrawal", variant: "warning" }, EVENT: { label: "Event", variant: "default" },
};

export function LedgerList({ rows, mode }: { rows: LedgerEntry[]; mode?: HistoryMode }) {
  if (rows.length === 0) return <p className="text-sm text-muted-foreground">{mode === "PAPER" ? "No paper trades yet." : mode === "LIVE" ? "No live transactions yet. Real trades and withdrawals appear here as they happen." : "No transactions yet."}</p>;
  return (
    <ul className="divide-y rounded-md border">
      {rows.map((r, i) => {
        const k = KIND_STYLE[r.kind];
        return (
          <li key={`${r.at}-${i}`} className="flex flex-wrap items-start justify-between gap-2 p-3 text-sm">
            <div className="min-w-0 space-y-0.5">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={k.variant}>{k.label}</Badge><span className="font-medium">{r.title}</span>
                {r.simulated && <Badge>PAPER</Badge>}
                {r.status && r.status !== "RECORDED" && r.status !== "FILLED" && <Badge variant={r.status === "FAILED" || r.status === "REJECTED" ? "destructive" : "warning"}>{r.status.replaceAll("_", " ")}</Badge>}
              </div>
              <p className="text-xs text-muted-foreground">{new Date(r.at).toLocaleString()} · {ago(r.at)}{r.fee_usdc ? ` · fee ${usd(r.fee_usdc)}` : ""}</p>
              {r.detail && <p className="break-words text-xs text-muted-foreground">{r.detail}</p>}
              {r.tx_hash && (r.explorer_url
                ? <a className="inline-flex items-center gap-1 text-xs underline" href={r.explorer_url} target="_blank" rel="noopener noreferrer">{shortAddr(r.tx_hash)}<ExternalLink className="h-3 w-3" aria-hidden /></a>
                : <code className="text-xs">{shortAddr(r.tx_hash)}</code>)}
            </div>
            {r.amount_usdc != null && <span className={`shrink-0 font-semibold tabular-nums ${pnlClass(r.amount_usdc)}`}>{signedUsd(r.amount_usdc)}</span>}
          </li>
        );
      })}
    </ul>
  );
}

function Stat({ label, value, sub, className }: { label: string; value: React.ReactNode; sub?: React.ReactNode; className?: string }) {
  return <div className={className}><p className="text-xs text-muted-foreground">{label}</p><p className="text-lg font-semibold tabular-nums">{value}</p>{sub && <p className="text-xs text-muted-foreground">{sub}</p>}</div>;
}

/** The agent wallet the way a user expects it: balance, deposit, withdraw, transaction history. */
export function AgentWalletOverview({ w, busy, onProvision, onChanged }: { w: WalletState; busy: boolean; onProvision: () => void; onChanged: () => void | Promise<void> }) {
  const { toast } = useToast();
  const paper = w.mode === "PAPER";
  const portfolio = useApi<Portfolio>("/portfolio", { refreshOn: ["POSITION_OPENED", "POSITION_CLOSED"], intervalMs: 20000 });
  const [histMode, setHistMode] = useState<HistoryMode | null>(null);
  const shownMode: HistoryMode = histMode ?? (paper ? "PAPER" : "LIVE");
  const ledger = useApi<LedgerEntry[]>(`/wallet/ledger?limit=100&mode=${shownMode}`, { refreshOn: ["POSITION_OPENED", "POSITION_CLOSED", "ORDER_FILLED"], intervalMs: 20000 });
  const [to, setTo] = useState("");
  const [amount, setAmount] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const cloud = w.cloud_wallet;
  const balance = w.usdc.balance;
  const problem = useMemo(() => validateWithdrawal(to, amount, balance, cloud?.address ?? null), [to, amount, balance, cloud?.address]);
  const explorer = w.network.explorer_url?.replace(/\/$/, "") ?? null;
  const p = portfolio.data;

  async function withdraw() {
    setSending(true); setError(null);
    try {
      const amt = Number(amount);
      const q = await api<{ command_id: string }>("/wallet/cloud/withdraw", { method: "POST", body: { to_address: to.trim(), amount_usdc: amt } });
      setConfirmOpen(false); setTo(""); setAmount("");
      const id = `withdraw-${q.command_id}`;
      toast({ id, kind: "loading", title: `Withdrawal of ${usd(amt)} requested`, description: "Your runner is submitting it…" });
      void (async () => {
        const out = await waitForCommand(q.command_id, { timeoutMs: 120_000 });
        if (out.status === "DONE") toast({ id, kind: "success", title: "Withdrawal submitted", description: out.detail || "The transfer was sent." });
        else if (out.status === "FAILED") toast({ id, kind: "error", title: "Withdrawal failed", description: out.detail || "The runner refused it." });
        else toast({ id, kind: "warning", title: "Withdrawal still pending", description: "No answer from the runner yet. Check the history below." });
        await onChanged(); await ledger.reload();
      })();
      await ledger.reload();
    } catch (e) { setError(toApiError(e)); } finally { setSending(false); }
  }

  return (
    <div className="space-y-4">
      {/* Balance */}
      <Card>
        <CardHeader><div className="flex flex-wrap items-center gap-2"><WalletIcon className="h-4 w-4" aria-hidden /><CardTitle>{paper ? "Paper wallet" : "Agent wallet"}</CardTitle>
          <Badge variant={paper ? "default" : "warning"}>{paper ? "VIRTUAL USDC" : "REAL USDC"}</Badge></div></CardHeader>
        <CardContent className="space-y-3">
          {paper ? (
            p ? (
              <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                <Stat label="Total value" value={usd(p.total_value_usdc)} sub={`started with ${usd(p.starting_cash_usdc)}`} />
                <Stat label="Available cash" value={usd(p.cash_usdc)} />
                <Stat label="In open positions" value={usd(p.exposure_usdc)} />
                <Stat label="Total P&L" value={<span className={pnlClass(p.realized_pnl_usdc + p.unrealized_pnl_usdc)}>{signedUsd(p.realized_pnl_usdc + p.unrealized_pnl_usdc)}</span>} sub={`today ${signedUsd(p.daily_pnl_usdc)}`} />
              </div>
            ) : <p className="text-sm text-muted-foreground">Loading paper balance…</p>
          ) : (
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <Stat label="USDC balance" value={balance == null ? "—" : usd(balance)} sub={w.usdc.view} />
              <Stat label="Allocated to the agent" value={w.allocated_capital_usdc == null ? "—" : usd(w.allocated_capital_usdc)} />
              <Stat label="Available to trade" value={w.available_trading_capital_usdc == null ? "—" : usd(w.available_trading_capital_usdc)} sub={w.capital_label} />
              <Stat label="Network" value={`Arc ${w.network.network}`} sub={w.network.health.ok ? "RPC reachable" : "RPC unavailable"} />
            </div>
          )}
          {!paper && w.usdc.note && <p className="text-xs text-muted-foreground">{w.usdc.note}</p>}
          {paper && <Alert>You are in PAPER mode: trades use virtual funds and nothing real moves. Deposits and withdrawals apply to the real agent wallet once you switch to LIVE on the Agent page.</Alert>}
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* Deposit */}
        <Card>
          <CardHeader><div className="flex items-center gap-2"><ArrowDownToLine className="h-4 w-4" aria-hidden /><CardTitle>Deposit</CardTitle></div></CardHeader>
          <CardContent className="space-y-3 text-sm">
            {cloud ? (<>
              <p>Send <strong>USDC on Arc {w.network.network}</strong> to your agent wallet address:</p>
              <code className="block break-all rounded bg-muted px-2 py-2 text-xs" data-testid="deposit-address">{cloud.address}</code>
              <div className="flex flex-wrap gap-2"><CopyButton text={cloud.address} />
                {explorer && <a className="inline-flex items-center gap-1 text-xs underline" href={`${explorer}/address/${cloud.address}`} target="_blank" rel="noopener noreferrer">View on explorer<ExternalLink className="h-3 w-3" aria-hidden /></a>}</div>
              <Alert variant="warning">Only send USDC on the Arc network (chain id {w.network.chain_id}). Tokens sent on another network, or a different token, can be lost.</Alert>
              <p className="text-xs text-muted-foreground">Your balance updates after the transfer confirms. LIVE trading stays blocked until the wallet is funded and your policy is set.</p>
            </>) : (<>
              <p className="text-muted-foreground">{paper ? "You do not need a wallet to paper trade." : "No agent wallet yet."} To trade with real funds, create the agent wallet; it gets its own deposit address.</p>
              <Button onClick={onProvision} disabled={busy}>Create agent wallet</Button>
              <p className="text-xs text-muted-foreground">Creates a Privy managed wallet and switches this account to the cloud runner.</p>
            </>)}
          </CardContent>
        </Card>

        {/* Withdraw */}
        <Card>
          <CardHeader><div className="flex items-center gap-2"><ArrowUpFromLine className="h-4 w-4" aria-hidden /><CardTitle>Withdraw</CardTitle></div></CardHeader>
          <CardContent className="space-y-3 text-sm">
            {cloud ? (<>
              <div className="space-y-1"><Label htmlFor="wd-to">Destination address</Label>
                <Input id="wd-to" placeholder="0x…" value={to} onChange={(e) => setTo(e.target.value)} autoComplete="off" spellCheck={false} /></div>
              <div className="space-y-1"><Label htmlFor="wd-amt">Amount (USDC)</Label>
                <div className="flex gap-2"><Input id="wd-amt" inputMode="decimal" placeholder="0.00" value={amount} onChange={(e) => setAmount(e.target.value)} />
                  <Button type="button" variant="outline" disabled={balance == null} onClick={() => balance != null && setAmount(String(balance))}>Max</Button></div></div>
              {(to || amount) && problem && <p className="text-xs text-warning" role="status">{problem}</p>}
              {error && <ErrorState error={error} />}
              <Button disabled={!!problem || sending} onClick={() => setConfirmOpen(true)}>Review withdrawal</Button>
              <p className="text-xs text-muted-foreground">Withdrawals run through your runner and stay disabled there until you explicitly enable them on it (the same safety gate LIVE trading uses). If they are off, the request is reported as failed with the reason.</p>
            </>) : <p className="text-muted-foreground">Withdrawals become available once you have an agent wallet with funds.</p>}
          </CardContent>
        </Card>
      </div>

      {/* History */}
      <Card>
        <CardHeader><div className="flex flex-wrap items-center gap-3"><div className="flex items-center gap-2"><History className="h-4 w-4" aria-hidden /><CardTitle>Transaction history</CardTitle></div>
          <ModeTabs value={shownMode} onChange={setHistMode} current={paper ? "PAPER" : "LIVE"} label="History mode" /></div></CardHeader>
        <CardContent className="space-y-3">
          <p className="text-xs text-muted-foreground">{shownMode === "PAPER" ? "Paper history: simulated trades with virtual money. There are no blockchain transactions, withdrawals or deposits here." : "Live history: real trades with their on-chain transaction links, plus withdrawals and wallet events."}</p>
          {ledger.error && !ledger.data ? <ErrorState error={ledger.error} onRetry={() => void ledger.reload()} /> : <LedgerList rows={ledger.data ?? []} mode={shownMode} />}
        </CardContent>
      </Card>

      <ConfirmDialog open={confirmOpen} onOpenChange={setConfirmOpen} busy={sending} destructive title="Send this withdrawal?" confirmLabel="Withdraw"
        description={`${amount ? usd(Number(amount)) : "—"} USDC will be sent from your agent wallet to ${to.trim() ? shortAddr(to.trim()) : "—"}. Blockchain transfers cannot be undone, so check the address.`}
        extra={<code className="block break-all rounded bg-muted p-2 text-xs">{to.trim()}</code>} onConfirm={withdraw} />
    </div>
  );
}
