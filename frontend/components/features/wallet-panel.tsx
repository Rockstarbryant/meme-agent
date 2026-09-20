"use client";
import { useState } from "react";
import { CheckCircle2, XCircle } from "lucide-react";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { ErrorState, Loading } from "@/components/states";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { useApi } from "@/hooks/use-api";
import { ApiError, api } from "@/lib/api";
import { shortAddr, usd } from "@/lib/format";
import { ensureArcNetwork, personalSign, requestAccount, sendSigningRequest } from "@/lib/wallet-browser";
import type { AuditLog, SigningRequest, WalletPolicy, WalletState } from "@/types/api";

const DEFAULT_POLICY: WalletPolicy = { allocated_capital_usdc: 500, max_trade_usdc: 25, max_position_usdc: 50, max_daily_loss_usdc: 50, max_open_positions: 5, max_slippage_pct: 2, min_liquidity_usdc: 10000 };
const FIELDS: { key: keyof WalletPolicy; label: string; step?: string }[] = [
  { key: "allocated_capital_usdc", label: "Allocated capital (USDC)" }, { key: "max_trade_usdc", label: "Maximum trade (USDC)" },
  { key: "max_position_usdc", label: "Maximum position (USDC)" }, { key: "max_daily_loss_usdc", label: "Daily loss limit (USDC)" },
  { key: "max_open_positions", label: "Maximum open positions", step: "1" }, { key: "max_slippage_pct", label: "Maximum slippage (%)", step: "0.1" },
  { key: "min_liquidity_usdc", label: "Minimum liquidity (USDC)" },
];

export function validatePolicy(p: WalletPolicy): string | null {
  const v = [p.allocated_capital_usdc, p.max_trade_usdc, p.max_position_usdc, p.max_daily_loss_usdc, p.max_open_positions, p.max_slippage_pct, p.min_liquidity_usdc];
  if (v.some((x) => !Number.isFinite(x) || x <= 0)) return "Every value must be a number greater than zero.";
  if (p.max_trade_usdc > p.max_position_usdc) return "Maximum trade cannot exceed maximum position.";
  if (p.max_position_usdc > p.allocated_capital_usdc) return "Maximum position cannot exceed allocated capital.";
  if (p.max_slippage_pct > 20) return "Maximum slippage cannot exceed 20%.";
  if (!Number.isInteger(p.max_open_positions)) return "Maximum open positions must be a whole number.";
  return null;
}

const walletMessage = (e: unknown): ApiError => (e instanceof ApiError ? e : new ApiError(0, (e as { message?: string })?.message ?? "Wallet request failed"));

export function WalletPanel() {
  const wallet = useApi<WalletState>("/wallet");
  const signing = useApi<SigningRequest[]>("/wallet/signing-requests");
  const activity = useApi<AuditLog[]>("/wallet/activity");
  const [policy, setPolicy] = useState<WalletPolicy | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [revokeOpen, setRevokeOpen] = useState(false);

  if (wallet.loading && !wallet.data) return <Loading />;
  if (!wallet.data) return wallet.error ? <ErrorState error={wallet.error} onRetry={() => void wallet.reload()} /> : null;
  const w = wallet.data;
  const form = policy ?? (w.policy ? { ...DEFAULT_POLICY, ...w.policy } : DEFAULT_POLICY);
  const problem = validatePolicy(form);
  const verified = w.wallets.find((x) => x.ownership_verified);

  async function run(name: string, fn: () => Promise<unknown>, ok?: string) {
    setBusy(name); setError(null); setNotice(null);
    try { await fn(); await wallet.reload(); await signing.reload(); await activity.reload(); if (ok) setNotice(ok); }
    catch (e) { setError(walletMessage(e)); } finally { setBusy(null); }
  }
  const connect = () => run("connect", async () => {
    const address = await requestAccount();
    await ensureArcNetwork(w.network.chain_params);
    const ch = await api<{ message: string }>("/wallet/challenge", { method: "POST", body: { address } });
    const signature = await personalSign(address, ch.message);
    await api("/wallet/connect", { method: "POST", body: { address, signature } });
  }, "Wallet connected. Ownership verified by signature; no funds moved.");
  const savePolicy = () => run("policy", () => api("/wallet/policy", { method: "POST", body: form }), "Policy saved. Existing authorizations were revoked; authorize again.");
  const authorize = () => run("authorize", () => api("/wallet/authorize", { method: "POST", body: { capability: "PER_TRADE_SIGNING", expires_in_hours: 24 } }), "Per-trade signing authorized for 24 hours.");
  const provisionCloud = () => run("cloud", () => api("/wallet/cloud/provision", { method: "POST", body: { confirm: true } }), "Privy cloud wallet provisioned. Fund the displayed address with Arc USDC before enabling LIVE.");
  const revoke = () => run("revoke", async () => { await api("/wallet/revoke", { method: "POST" }); setRevokeOpen(false); }, "Authorization revoked. Also revoke token approvals inside your wallet.");
  const signRequest = (r: SigningRequest) => run(`sign-${r.id}`, async () => {
    if (!verified) throw new ApiError(0, "Connect a wallet first.");
    const hash = await sendSigningRequest(verified.address, r);
    await api(`/wallet/signing-requests/${r.id}/complete`, { method: "POST", body: { tx_hash: hash } });
  });

  const cap = w.execution_capability.capability;
  return (
    <div className="space-y-4">
      {error && <ErrorState error={error} />}
      {notice && <Alert variant="success">{notice}</Alert>}

      <Card>
        <CardHeader><CardTitle>Execution capability</CardTitle></CardHeader>
        <CardContent className="space-y-2 text-sm">
          <p className="text-base font-semibold">{w.execution_capability.label}</p>
          <p className="text-muted-foreground">{w.execution_capability.detail}</p>
          <ul className="space-y-1">
            <li className="flex items-start gap-2">{w.execution_mode === "cloud_managed" ? <CheckCircle2 className="mt-0.5 h-4 w-4 text-success" aria-hidden /> : <XCircle className="mt-0.5 h-4 w-4 text-muted-foreground" aria-hidden />}<span><strong>Autonomous cloud execution:</strong> {w.execution_mode === "cloud_managed" ? "uses your per-user Privy managed wallet on the shared cloud worker." : "not enabled for this account."}</span></li>
            <li className="flex items-start gap-2">{cap === "PER_TRADE_SIGNING" ? <CheckCircle2 className="mt-0.5 h-4 w-4 text-success" aria-hidden /> : <XCircle className="mt-0.5 h-4 w-4 text-muted-foreground" aria-hidden />}<span><strong>Explicit per-trade signing:</strong> {cap === "PER_TRADE_SIGNING" ? "active" : "inactive (connect, set a policy, authorize)"}</span></li>
            <li className="flex items-start gap-2"><CheckCircle2 className="mt-0.5 h-4 w-4 text-success" aria-hidden /><span><strong>Paper trading:</strong> always available, no wallet required</span></li>
          </ul>
          {w.live_blockers.length > 0 && <p className="text-xs text-muted-foreground">LIVE blocked: {w.live_blockers.join("; ")}</p>}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Network and balance</CardTitle></CardHeader>
        <CardContent className="space-y-2 text-sm">
          <p>Arc {w.network.network} (chain id {w.network.chain_id}) — RPC {w.network.health.ok ? <Badge variant="success">reachable</Badge> : <Badge variant="destructive">unavailable</Badge>}{!w.network.health.ok && w.network.health.detail ? ` (${w.network.health.detail})` : ""}</p>
          <p>USDC balance: <strong>{w.usdc.balance == null ? "—" : usd(w.usdc.balance)}</strong> <span className="text-xs text-muted-foreground">({w.usdc.view})</span></p>
          <p className="text-xs text-muted-foreground">{w.usdc.note}</p>
          <p>Allocated capital: {w.allocated_capital_usdc == null ? "—" : usd(w.allocated_capital_usdc)} · Available trading capital: {w.available_trading_capital_usdc == null ? "—" : usd(w.available_trading_capital_usdc)} <Badge>{w.capital_label}</Badge></p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Cloud agent wallet</CardTitle></CardHeader>
        <CardContent className="space-y-2 text-sm">
          {w.cloud_wallet ? (<><p>Provider: <strong>Privy</strong> · {w.cloud_wallet.active ? <Badge variant="success">cloud execution enabled</Badge> : <Badge variant="warning">provisioned, not active</Badge>}</p><p>Address: <code>{shortAddr(w.cloud_wallet.address)}</code></p><p className="text-xs text-muted-foreground">This is a platform app-scoped managed wallet. Privy holds the wallet key material; the shared worker can act only when the account, worker, application policy, and LIVE gates permit it.</p></>) : <><p className="text-muted-foreground">No Privy cloud wallet is provisioned.</p><Button onClick={() => void provisionCloud()} disabled={busy !== null}>Provision Privy cloud wallet</Button><p className="text-xs text-muted-foreground">Provisioning switches this account to the cloud-managed execution mode and creates a separate Privy wallet.</p></>}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Wallet on your Local Runner</CardTitle></CardHeader>
        <CardContent className="space-y-2 text-sm">
          {w.runner_wallet === null ? <p className="text-muted-foreground">No runner is paired, so there is no autonomous wallet. Pair one on the Agent page.</p> : (<>
            <p>Provider: <strong>{w.runner_wallet.provider ?? "none (PAPER only)"}</strong> · runner {w.runner_wallet.online ? <Badge variant="success">online</Badge> : <Badge variant="warning">offline</Badge>}</p>
            {w.runner_wallet.live && !w.runner_wallet.live.available && (<div><p className="text-xs text-muted-foreground">LIVE is unavailable because:</p>
              <ul className="list-disc pl-5 text-xs text-muted-foreground">{w.runner_wallet.live.reasons.map((r) => <li key={r}>{r}</li>)}</ul></div>)}
          </>)}
          <p className="text-xs text-muted-foreground">Browser wallet keys stay in your wallet. A Privy cloud wallet uses a separate platform-managed custody model; see the cloud wallet disclosure above.</p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Browser wallet (optional, manual signing)</CardTitle></CardHeader>
        <CardContent className="space-y-2 text-sm">
          {w.wallets.length === 0 && <p className="text-muted-foreground">No wallet connected.</p>}
          {w.wallets.map((x) => <p key={x.id}>{x.provider}: <code>{shortAddr(x.address)}</code> {x.ownership_verified ? <Badge variant="success">ownership verified</Badge> : <Badge>paper placeholder</Badge>}</p>)}
          <Button onClick={() => void connect()} disabled={busy !== null}>{verified ? "Reconnect / switch account" : "Connect wallet"}</Button>
          <p className="text-xs text-muted-foreground">Connecting asks your wallet to switch to Arc and sign a one-time message. It never asks for a private key and moves no funds.</p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Trading policy</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            {FIELDS.map((f) => (
              <div key={f.key} className="space-y-1"><Label htmlFor={f.key}>{f.label}</Label>
                <Input id={f.key} type="number" inputMode="decimal" step={f.step ?? "any"} value={Number.isNaN(form[f.key]) ? "" : form[f.key]}
                  onChange={(e) => setPolicy({ ...form, [f.key]: e.target.value === "" ? NaN : Number(e.target.value) })} /></div>
            ))}
          </div>
          {problem && <Alert variant="warning">{problem}</Alert>}
          <div className="flex flex-wrap gap-2">
            <Button disabled={!!problem || busy !== null} onClick={() => void savePolicy()}>Save policy</Button>
            <Button variant="outline" disabled={!verified || !w.policy || busy !== null} onClick={() => void authorize()}>Authorize per-trade signing</Button>
            <Button variant="outline" disabled title="Not available: see Circle Agent Wallet below">Authorize autonomous delegation</Button>
            <Button variant="destructive" disabled={!w.authorization || busy !== null} onClick={() => setRevokeOpen(true)}>Revoke authorization</Button>
          </div>
          <p className="text-xs text-muted-foreground">Changing the policy revokes existing authorizations. Application risk limits always apply on top of wallet limits.</p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Circle Agent Wallet and other options</CardTitle></CardHeader>
        <CardContent className="space-y-3 text-sm">
          <Alert variant="warning"><p className="font-medium">{w.agent_wallet.product}: not integrated</p><p className="mt-1">{w.agent_wallet.reason}</p></Alert>
          <div className="grid gap-3 md:grid-cols-2">
            <div><p className="mb-1 text-xs font-semibold">Documented by Circle</p><ul className="list-disc pl-5 text-xs text-muted-foreground">{w.agent_wallet.documented.map((d) => <li key={d}>{d}</li>)}</ul></div>
            <div><p className="mb-1 text-xs font-semibold">Not documented (unverified)</p><ul className="list-disc pl-5 text-xs text-muted-foreground">{w.agent_wallet.not_documented.map((d) => <li key={d}>{d}</li>)}</ul></div>
          </div>
          <p className="text-xs text-muted-foreground">Decision needed: see {w.agent_wallet.decision_doc}. Funding an agent wallet is unavailable until that is settled; PAPER uses virtual USDC.</p>
          <ul className="divide-y rounded-md border">
            {w.wallet_options.map((o) => (
              <li key={o.id} className="flex flex-wrap items-start justify-between gap-2 p-2"><div><p className="font-medium">{o.label}</p><p className="text-xs text-muted-foreground">{o.note}</p></div>
                <Badge variant={o.status === "available" ? "success" : o.status === "forbidden" ? "destructive" : "warning"}>{o.status === "forbidden" ? "Forbidden (custodial)" : o.status === "available" ? "Available" : "Not integrated"}</Badge></li>
            ))}
          </ul>
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Signing requests</CardTitle></CardHeader>
        <CardContent className="space-y-2 text-sm">
          {(signing.data ?? []).length === 0 && <p className="text-muted-foreground">No pending requests. In LIVE mode each trade appears here for your wallet to sign.</p>}
          {(signing.data ?? []).map((r) => (
            <div key={r.id} className="flex items-center justify-between gap-2 rounded-md border p-2"><span>{r.tx.side} {usd(r.tx.amount_usdc)} · {r.status}</span>
              {r.status === "PENDING" && <Button size="sm" disabled={busy !== null} onClick={() => void signRequest(r)}>Review and sign</Button>}</div>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Wallet activity</CardTitle></CardHeader>
        <CardContent><ul className="space-y-1 text-xs">{(activity.data ?? []).map((a, i) => <li key={`${a.at}-${i}`}><span className="text-muted-foreground">{new Date(a.at).toLocaleString()}</span> {a.action}</li>)}
          {(activity.data ?? []).length === 0 && <li className="text-muted-foreground">No wallet activity yet.</li>}</ul></CardContent>
      </Card>

      <ConfirmDialog open={revokeOpen} onOpenChange={setRevokeOpen} destructive busy={busy !== null} title="Revoke authorization?" confirmLabel="Revoke"
        description="The agent immediately loses permission to queue trades. If LIVE is on and no positions are open it switches back to PAPER." onConfirm={revoke} />
    </div>
  );
}
