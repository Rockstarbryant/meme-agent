const DASH = "—";
export const usd = (n: number | null | undefined, digits = 2) =>
  n == null ? DASH : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits }).format(n);
export const signedUsd = (n: number | null | undefined) => (n == null ? DASH : `${n > 0 ? "+" : ""}${usd(n)}`);
export const compact = (n: number | null | undefined) =>
  n == null ? DASH : new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(n);
export const pct = (n: number | null | undefined, digits = 1) => (n == null ? DASH : `${n > 0 ? "+" : ""}${n.toFixed(digits)}%`);
export const plainPct = (n: number | null | undefined, digits = 1) => (n == null ? DASH : `${n.toFixed(digits)}%`);
export const price = (n: number | null | undefined) => {
  if (n == null) return DASH;
  const d = n >= 1 ? 4 : n >= 0.01 ? 5 : 8;
  return `$${n.toFixed(d)}`;
};
export const num = (n: number | null | undefined, digits = 2) => (n == null ? DASH : n.toFixed(digits));
export const shortAddr = (a: string) => (a.length > 12 ? `${a.slice(0, 6)}…${a.slice(-4)}` : a);
export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return DASH;
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}
export function ago(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return DASH;
  return `${duration(Math.max(0, (now - new Date(iso).getTime()) / 1000))} ago`;
}
export const pnlClass = (n: number | null | undefined) =>
  n == null || n === 0 ? "text-muted-foreground" : n > 0 ? "text-success" : "text-destructive";
