"use client";
import { useId, useMemo, useRef, useState } from "react";
import { axisTime, monotonePath, nearestIndex, niceTicks, type Range } from "@/lib/chart";
import { usd } from "@/lib/format";

export interface SeriesPoint { t: number; v: number }

interface Props {
  points: SeriesPoint[];
  range: Range;
  /** Value the line is judged against (the starting value): above = green, below = red, drawn as a dashed line. */
  baseline?: number | null;
  height?: number;
  label: string;
  format?: (v: number) => string;
}

const W = 640;   // viewBox width; the SVG scales to its container
const PAD = { l: 8, r: 52, t: 14, b: 22 };

/** Interactive area chart: smooth line, gradient fill, baseline, touch / hover crosshair with a value tooltip. */
export function AreaChart({ points, range, baseline = null, height = 220, label, format = (v) => usd(v) }: Props) {
  const gid = useId().replace(/:/g, "");
  const ref = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const H = height;

  const geo = useMemo(() => {
    if (points.length < 2) return null;
    const vals = points.map((p) => p.v);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (baseline != null) { lo = Math.min(lo, baseline); hi = Math.max(hi, baseline); }
    const span = hi - lo || Math.abs(hi) * 0.02 || 1;
    lo -= span * 0.12; hi += span * 0.12;
    const t0 = points[0].t, t1 = points[points.length - 1].t;
    const x = (t: number) => PAD.l + ((t - t0) / (t1 - t0 || 1)) * (W - PAD.l - PAD.r);
    const y = (v: number) => PAD.t + (1 - (v - lo) / (hi - lo)) * (H - PAD.t - PAD.b);
    const xy = points.map((p) => ({ x: x(p.t), y: y(p.v) }));
    const line = monotonePath(xy);
    const area = `${line}L${xy[xy.length - 1].x},${H - PAD.b}L${xy[0].x},${H - PAD.b}Z`;
    const ticks = niceTicks(lo, hi, 4).filter((v) => v >= lo && v <= hi);
    const xt = [0, 0.25, 0.5, 0.75, 1].map((f) => t0 + (t1 - t0) * f);
    return { xy, line, area, y, x, ticks, xt, xs: xy.map((p) => p.x) };
  }, [points, baseline, H]);

  if (!geo) {
    return <div className="flex items-center justify-center rounded-md border border-dashed text-sm text-muted-foreground" style={{ height }}>
      Not enough history for this range yet. Points appear as the agent runs.</div>;
  }
  const first = points[0].v, last = points[points.length - 1].v;
  const ref0 = baseline ?? first;
  const up = last >= ref0;
  const color = up ? "hsl(var(--success))" : "hsl(var(--destructive))";
  const active = hover != null ? points[hover] : null;
  const activeXY = hover != null ? geo.xy[hover] : null;

  function move(clientX: number) {
    const svg = ref.current;
    if (!svg) return;
    const r = svg.getBoundingClientRect();
    const x = ((clientX - r.left) / r.width) * W;
    setHover(nearestIndex(geo!.xs, x));
  }
  const tipLeft = activeXY ? Math.min(Math.max(activeXY.x / W, 0.18), 0.82) * 100 : 0;
  const delta = active ? active.v - ref0 : null;

  return (
    <div className="relative select-none">
      <svg ref={ref} viewBox={`0 0 ${W} ${H}`} className="w-full touch-pan-y" role="img" aria-label={`${label}: from ${format(first)} to ${format(last)}`}
        onPointerMove={(e) => move(e.clientX)} onPointerDown={(e) => move(e.clientX)} onPointerLeave={() => setHover(null)}>
        <defs>
          <linearGradient id={`g${gid}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.38" /><stop offset="100%" stopColor={color} stopOpacity="0" />
          </linearGradient>
        </defs>
        {geo.ticks.map((v) => (
          <g key={v}><line x1={PAD.l} x2={W - PAD.r} y1={geo.y(v)} y2={geo.y(v)} stroke="hsl(var(--border))" strokeOpacity="0.5" strokeDasharray="2 5" />
            <text x={W - PAD.r + 6} y={geo.y(v) + 3} fontSize="10" fill="hsl(var(--muted-foreground))">{format(v).replace(/\.00$/, "")}</text></g>
        ))}
        {baseline != null && <g><line x1={PAD.l} x2={W - PAD.r} y1={geo.y(baseline)} y2={geo.y(baseline)} stroke="hsl(var(--muted-foreground))" strokeOpacity="0.8" strokeDasharray="5 4" />
          <text x={PAD.l + 2} y={geo.y(baseline) - 4} fontSize="10" fill="hsl(var(--muted-foreground))">start</text></g>}
        <path d={geo.area} fill={`url(#g${gid})`} />
        <path d={geo.line} fill="none" stroke={color} strokeWidth="2.25" strokeLinejoin="round" strokeLinecap="round" />
        <circle cx={geo.xy[geo.xy.length - 1].x} cy={geo.xy[geo.xy.length - 1].y} r="4" fill={color} />
        <circle cx={geo.xy[geo.xy.length - 1].x} cy={geo.xy[geo.xy.length - 1].y} r="9" fill={color} fillOpacity="0.2" />
        {geo.xt.map((t, i) => (
          <text key={i} x={Math.min(Math.max(geo.x(t), 18), W - PAD.r - 16)} y={H - 6} fontSize="10" textAnchor="middle" fill="hsl(var(--muted-foreground))">{axisTime(t, range)}</text>))}
        {activeXY && <g><line x1={activeXY.x} x2={activeXY.x} y1={PAD.t} y2={H - PAD.b} stroke="hsl(var(--foreground))" strokeOpacity="0.35" />
          <circle cx={activeXY.x} cy={activeXY.y} r="5" fill="hsl(var(--background))" stroke={color} strokeWidth="2.5" /></g>}
      </svg>
      {active && delta != null && (
        <div className="pointer-events-none absolute top-1 -translate-x-1/2 rounded-md border bg-background px-2 py-1 text-xs shadow-md" style={{ left: `${tipLeft}%` }}>
          <p className="font-semibold tabular-nums">{format(active.v)}</p>
          <p className={`tabular-nums ${delta >= 0 ? "text-success" : "text-destructive"}`}>{delta >= 0 ? "+" : ""}{format(delta)} vs start</p>
          <p className="text-muted-foreground">{new Date(active.t * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</p>
        </div>)}
    </div>
  );
}

/** Tiny trend line for KPI tiles. */
export function Sparkline({ values, up, width = 84, height = 28 }: { values: number[]; up: boolean; width?: number; height?: number }) {
  const d = useMemo(() => {
    if (values.length < 2) return null;
    const lo = Math.min(...values), hi = Math.max(...values), span = hi - lo || 1;
    return monotonePath(values.map((v, i) => ({ x: 2 + (i / (values.length - 1)) * (width - 4), y: 2 + (1 - (v - lo) / span) * (height - 4) })));
  }, [values, width, height]);
  if (!d) return <span className="inline-block" style={{ width, height }} aria-hidden />;
  return <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden>
    <path d={d} fill="none" stroke={up ? "hsl(var(--success))" : "hsl(var(--destructive))"} strokeWidth="1.75" strokeLinecap="round" /></svg>;
}
