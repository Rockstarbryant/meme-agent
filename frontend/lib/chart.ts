/** Pure helpers for the SVG charts (kept free of React so they can be unit-tested). */

export interface Pt { x: number; y: number }

/** "Nice" axis ticks covering [min, max]: 1/2/5 x 10^n steps, about ``count`` of them. */
export function niceTicks(min: number, max: number, count = 4): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [];
  if (min === max) { const pad = Math.abs(min) * 0.01 || 1; min -= pad; max += pad; }
  const rough = (max - min) / Math.max(1, count);
  const mag = 10 ** Math.floor(Math.log10(rough));
  const norm = rough / mag;
  const step = (norm <= 1.5 ? 1 : norm <= 3 ? 2 : norm <= 7 ? 5 : 10) * mag;   // rounds to the nearest 1/2/5 step, so 3 to 6 ticks
  const out: number[] = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) out.push(Number(v.toPrecision(12)));
  return out;
}

/** Smooth path through the points (monotone cubic / Fritsch-Carlson): never overshoots, so a flat stretch stays flat
 *  and a curve never dips below the data between two samples. */
export function monotonePath(pts: Pt[]): string {
  const n = pts.length;
  if (n === 0) return "";
  if (n === 1) return `M${pts[0].x},${pts[0].y}`;
  if (n === 2) return `M${pts[0].x},${pts[0].y}L${pts[1].x},${pts[1].y}`;
  const dx: number[] = [], dy: number[] = [], m: number[] = [];
  for (let i = 0; i < n - 1; i++) { dx.push(pts[i + 1].x - pts[i].x); dy.push(pts[i + 1].y - pts[i].y); m.push(dx[i] === 0 ? 0 : dy[i] / dx[i]); }
  const t: number[] = [m[0]];
  for (let i = 1; i < n - 1; i++) t.push(m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2);
  t.push(m[n - 2]);
  for (let i = 0; i < n - 1; i++) {
    if (m[i] === 0) { t[i] = 0; t[i + 1] = 0; continue; }
    const a = t[i] / m[i], b = t[i + 1] / m[i], h = a * a + b * b;
    if (h > 9) { const k = 3 / Math.sqrt(h); t[i] = k * a * m[i]; t[i + 1] = k * b * m[i]; }
  }
  let d = `M${pts[0].x},${pts[0].y}`;
  for (let i = 0; i < n - 1; i++) {
    const c1x = pts[i].x + dx[i] / 3, c1y = pts[i].y + (t[i] * dx[i]) / 3;
    const c2x = pts[i + 1].x - dx[i] / 3, c2y = pts[i + 1].y - (t[i + 1] * dx[i]) / 3;
    d += `C${c1x},${c1y} ${c2x},${c2y} ${pts[i + 1].x},${pts[i + 1].y}`;
  }
  return d;
}

/** Index of the sample whose x is closest to ``x`` (binary search; xs ascending). */
export function nearestIndex(xs: number[], x: number): number {
  if (xs.length === 0) return -1;
  let lo = 0, hi = xs.length - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (xs[mid] < x) lo = mid; else hi = mid; }
  return Math.abs(xs[lo] - x) <= Math.abs(xs[hi] - x) ? lo : hi;
}

export type Range = "1h" | "6h" | "24h" | "7d" | "all";
/** Axis label for a unix-seconds timestamp, sized to the range being shown. */
export function axisTime(t: number, range: Range, locale?: string): string {
  const d = new Date(t * 1000);
  if (range === "1h" || range === "6h" || range === "24h") return d.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString(locale, { day: "numeric", month: "short" });
}

/** Percent change from ``base``; null when it cannot be computed. */
export function changePct(base: number | null | undefined, now: number | null | undefined): number | null {
  if (base == null || now == null || !Number.isFinite(base) || !Number.isFinite(now) || base === 0) return null;
  return ((now - base) / Math.abs(base)) * 100;
}

/* ------------------------------------------------------------------ candles / time axis */
export type Tf = "1m" | "5m" | "15m" | "1h" | "4h" | "1d";
export const TF_SECONDS: Record<Tf, number> = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400 };

/** Axis label for a unix-seconds timestamp: time of day for intraday spans, date for longer ones. */
export function candleAxisLabel(tSec: number, spanSec: number, locale?: string): string {
  const d = new Date(tSec * 1000);
  if (spanSec <= 36 * 3600) return d.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", hour12: false });
  if (spanSec <= 14 * 86400) return `${d.toLocaleDateString(locale, { month: "short", day: "numeric" })} ${d.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", hour12: false })}`;
  return d.toLocaleDateString(locale, { month: "short", day: "numeric" });
}

/** Full timestamp for the crosshair tooltip. */
export const candleFullTime = (tSec: number, locale?: string) =>
  new Date(tSec * 1000).toLocaleString(locale, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });

/** Evenly spaced x-axis tick indices (always includes first and last candle). */
export function xTickIndices(n: number, count = 5): number[] {
  if (n <= 0) return [];
  if (n <= count) return Array.from({ length: n }, (_, i) => i);
  return Array.from({ length: count }, (_, i) => Math.round((i * (n - 1)) / (count - 1)));
}

/** Price with enough significant digits for sub-cent meme tokens (0.00055409 stays readable, 1.2345 stays short). */
export function fmtPrice(v: number): string {
  if (!Number.isFinite(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (a >= 1) return v.toFixed(4);
  if (a === 0) return "0";
  const digits = Math.min(10, 3 - Math.floor(Math.log10(a)) + 1);
  return v.toFixed(digits);
}
