"use client";
import { useState } from "react";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { ErrorState, Loading } from "@/components/states";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { useApi } from "@/hooks/use-api";
import { api, toApiError, type ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { RiskLimits } from "@/types/api";

const FIELDS: { key: keyof RiskLimits; label: string; step?: string }[] = [
  { key: "max_trade_usdc", label: "Maximum trade (USDC)" }, { key: "max_position_usdc", label: "Maximum position (USDC)" },
  { key: "max_daily_loss_usdc", label: "Maximum daily loss (USDC)" }, { key: "max_total_exposure_usdc", label: "Maximum total exposure (USDC)" },
  { key: "max_open_positions", label: "Maximum open positions", step: "1" }, { key: "max_slippage_pct", label: "Maximum slippage (%)", step: "0.1" },
  { key: "min_liquidity_usdc", label: "Minimum liquidity (USDC)" },
];
type Editable = Pick<RiskLimits, "max_trade_usdc" | "max_position_usdc" | "max_daily_loss_usdc" | "max_total_exposure_usdc" | "max_open_positions" | "max_slippage_pct" | "min_liquidity_usdc">;

export function validateLimits(l: Editable): string | null {
  const nums = [l.max_trade_usdc, l.max_position_usdc, l.max_daily_loss_usdc, l.max_total_exposure_usdc, l.max_open_positions, l.max_slippage_pct, l.min_liquidity_usdc];
  if (nums.some((v) => !Number.isFinite(v) || v <= 0)) return "Every value must be a number greater than zero.";
  if (l.max_trade_usdc > l.max_position_usdc) return "Maximum trade cannot exceed maximum position.";
  if (l.max_position_usdc > l.max_total_exposure_usdc) return "Maximum position cannot exceed maximum total exposure.";
  if (l.max_slippage_pct > 20) return "Maximum slippage cannot exceed 20%.";
  if (!Number.isInteger(l.max_open_positions)) return "Maximum open positions must be a whole number.";
  return null;
}

export function RiskLimitsForm() {
  const { user } = useAuth();
  const res = useApi<{ limits: RiskLimits; effective_limits: RiskLimits }>("/risk/limits");
  const [edits, setEdits] = useState<Partial<Editable>>({});
  const [error, setError] = useState<ApiError | null>(null);
  const [saved, setSaved] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);

  if (res.loading && !res.data) return <Loading />;
  if (!res.data) return res.error ? <ErrorState error={res.error} onRetry={() => void res.reload()} /> : null;
  const base = res.data.limits;
  const values = { ...base, ...edits } as RiskLimits;
  const problem = validateLimits(values);
  const live = user?.mode === "LIVE";

  async function save() {
    setBusy(true); setError(null); setSaved(false);
    try { await api("/risk/limits", { method: "PUT", body: { ...values, confirm: live } }); setSaved(true); setEdits({}); setConfirm(false); await res.reload(); }
    catch (e) { setError(toApiError(e)); } finally { setBusy(false); }
  }
  return (
    <Card>
      <CardHeader><CardTitle>Risk limits</CardTitle></CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2">
          {FIELDS.map((f) => (
            <div key={f.key} className="space-y-1"><Label htmlFor={`rl-${f.key}`}>{f.label}</Label>
              <Input id={`rl-${f.key}`} type="number" inputMode="decimal" step={f.step ?? "any"}
                value={Number.isNaN(values[f.key] as number) ? "" : (values[f.key] as number)}
                onChange={(e) => setEdits({ ...edits, [f.key]: e.target.value === "" ? NaN : Number(e.target.value) })} /></div>
          ))}
        </div>
        {problem && <Alert variant="warning">{problem}</Alert>}
        {error && <ErrorState error={error} />}
        {saved && <Alert variant="success">Risk limits saved. The agent restarted in STOPPED state; open positions remain protected.</Alert>}
        <Button disabled={!!problem || busy || Object.keys(edits).length === 0} onClick={() => (live ? setConfirm(true) : void save())}>Save risk limits</Button>
        <p className="text-xs text-muted-foreground">The AI can never change these. Effective limits are the stricter of these and your wallet policy (max trade now {res.data.effective_limits.max_trade_usdc} USDC).</p>
        <ConfirmDialog open={confirm} onOpenChange={setConfirm} busy={busy} destructive title="Change LIVE risk limits?" confirmLabel="Save limits"
          description="You are in LIVE mode. Changing limits affects real funds and restarts the agent in STOPPED state." onConfirm={save} />
      </CardContent>
    </Card>
  );
}
