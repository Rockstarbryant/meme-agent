"use client";
import { useMemo, useRef, useState } from "react";
import { candleAxisLabel, candleFullTime, fmtPrice, niceTicks, xTickIndices } from "@/lib/chart";
import { compact } from "@/lib/format";
import type { Candle } from "@/types/api";

interface Props {
  candles: Candle[];
  height?: number;
  showVolume?: boolean;
  /** Entry price of an open position: drawn as a dashed line. */
  entryPrice?: number | null;
  label?: string;
}

const W = 720;                                   // viewBox width; the SVG scales to its container
const PAD = { l: 8, r: 64, t: 12, b: 24 };

/** Dependency-free candlestick chart: OHLC bodies + wicks, volume bars, time axis, last-price line, crosshair with full OHLCV tooltip. */
export function CandleChart({ candles, height = 300, showVolume = true, entryPrice = null, label = "price chart" }: Props) {
  const ref = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const hasVol = showVolume && candles.some((c) => c.v > 0);
  const volH = hasVol ? Math.round(height * 0.2) : 0;
  const priceB = height - PAD.b - volH - (hasVol ? 6 : 0);

  const geo = useMemo(() => {
    if (candles.length < 2) return null;
    let lo = Math.min(...candles.map((c) => c.l)), hi = Math.max(...candles.map((c) => c.h));
    if (entryPrice != null && entryPrice > 0) { lo = Math.min(lo, entryPrice); hi = Math.max(hi, entryPrice); }
    const span = hi - lo || hi * 0.02 || 1;
    lo -= span * 0.06; hi += span * 0.06;
    const n = candles.length, plotW = W - PAD.l - PAD.r, step = plotW / n;
    const bodyW = Math.max(1.5, Math.min(14, step * 0.68));
    const cx = (i: number) => PAD.l + step * (i + 0.5);
    const y = (v: number) => PAD.t + (1 - (v - lo) / (hi - lo)) * (priceB - PAD.t);
    const vmax = Math.max(...candles.map((c) => c.v), 0) || 1;
    const vy = (v: number) => height - PAD.b - (v / vmax) * volH;
    const span_s = candles[n - 1].t - candles[0].t;
    return { n, step, bodyW, cx, y, vy, ticks: niceTicks(lo, hi, 4).filter((t) => t >= lo && t <= hi), xt: xTickIndices(n, 5), span_s };
  }, [candles, entryPrice, height, priceB, volH]);

  if (!geo) {
    return <div className="flex items-center justify-center rounded-md border border-dashed border-border-hover px-4 text-center text-sm text-muted-foreground" style={{ height }}>
      Not enough candles for this timeframe yet.</div>;
  }
  const last = candles[candles.length - 1], first = candles[0];
  const upAll = last.c >= first.o;
  const h = hover != null ? candles[hover] : null;

  const onMove = (clientX: number) => {
    const r = ref.current?.getBoundingClientRect();
    if (!r) return;
    const x = ((clientX - r.left) / r.width) * W;
    const i = Math.round((x - PAD.l) / geo.step - 0.5);
    setHover(Math.max(0, Math.min(geo.n - 1, i)));
  };
  const tipRight = hover != null && geo.cx(hover) < W * 0.55;

  return (
    <div className="relative">
      <svg ref={ref} role="img" aria-label={label} viewBox={`0 0 ${W} ${height}`} className="w-full touch-pan-y select-none"
        onMouseMove={(e) => onMove(e.clientX)} onMouseLeave={() => setHover(null)}
        onTouchStart={(e) => onMove(e.touches[0].clientX)} onTouchMove={(e) => onMove(e.touches[0].clientX)} onTouchEnd={() => setHover(null)}>
        {geo.ticks.map((t) => (
          <g key={t}>
            <line x1={PAD.l} x2={W - PAD.r} y1={geo.y(t)} y2={geo.y(t)} className="stroke-border" strokeDasharray="2 4" strokeWidth={1} />
            <text x={W - PAD.r + 6} y={geo.y(t) + 3.5} className="fill-muted-foreground" fontSize={10}>{fmtPrice(t)}</text>
          </g>))}
        {geo.xt.map((i) => (
          <g key={i}>
            <line x1={geo.cx(i)} x2={geo.cx(i)} y1={PAD.t} y2={height - PAD.b} className="stroke-border/40" strokeWidth={1} />
            <text x={geo.cx(i)} y={height - 7} textAnchor={i === 0 ? "start" : i === geo.n - 1 ? "end" : "middle"} className="fill-muted-foreground" fontSize={10}>
              {candleAxisLabel(candles[i].t, geo.span_s)}</text>
          </g>))}
        {entryPrice != null && entryPrice > 0 && (
          <g><line x1={PAD.l} x2={W - PAD.r} y1={geo.y(entryPrice)} y2={geo.y(entryPrice)} className="stroke-accent" strokeDasharray="5 4" strokeWidth={1} />
            <text x={PAD.l + 4} y={geo.y(entryPrice) - 4} className="fill-accent" fontSize={10}>entry {fmtPrice(entryPrice)}</text></g>)}
        {hasVol && candles.map((c, i) => (
          <rect key={`v${c.t}`} x={geo.cx(i) - geo.bodyW / 2} width={geo.bodyW} y={geo.vy(c.v)} height={Math.max(0, height - PAD.b - geo.vy(c.v))}
            className={c.c >= c.o ? "fill-success/35" : "fill-destructive/35"} />))}
        {candles.map((c, i) => {
          const up = c.c >= c.o, top = geo.y(Math.max(c.o, c.c)), bot = geo.y(Math.min(c.o, c.c));
          return (
            <g key={c.t} className={up ? "stroke-success fill-success" : "stroke-destructive fill-destructive"}>
              <line x1={geo.cx(i)} x2={geo.cx(i)} y1={geo.y(c.h)} y2={geo.y(c.l)} strokeWidth={1} />
              <rect x={geo.cx(i) - geo.bodyW / 2} width={geo.bodyW} y={top} height={Math.max(1, bot - top)} strokeWidth={0.5} />
            </g>);
        })}
        <line x1={PAD.l} x2={W - PAD.r} y1={geo.y(last.c)} y2={geo.y(last.c)} className={upAll ? "stroke-success" : "stroke-destructive"} strokeDasharray="1 3" strokeWidth={1} />
        <rect x={W - PAD.r + 2} y={geo.y(last.c) - 8} width={PAD.r - 4} height={16} rx={3} className={upAll ? "fill-success" : "fill-destructive"} />
        <text x={W - PAD.r + 5} y={geo.y(last.c) + 3.5} fontSize={9.5} className="fill-background">{fmtPrice(last.c)}</text>
        {hover != null && (
          <g pointerEvents="none">
            <line x1={geo.cx(hover)} x2={geo.cx(hover)} y1={PAD.t} y2={height - PAD.b} className="stroke-foreground/50" strokeWidth={1} />
            <line x1={PAD.l} x2={W - PAD.r} y1={geo.y(candles[hover].c)} y2={geo.y(candles[hover].c)} className="stroke-foreground/30" strokeWidth={1} strokeDasharray="3 3" />
          </g>)}
      </svg>
      {h && (
        <div className={`pointer-events-none absolute top-1 z-10 rounded-md border bg-card/95 px-2.5 py-1.5 text-xs shadow-sm ${tipRight ? "right-2" : "left-2"}`}>
          <p className="font-medium">{candleFullTime(h.t)}</p>
          <p className="display-num text-muted-foreground">O {fmtPrice(h.o)} · H {fmtPrice(h.h)}</p>
          <p className="display-num text-muted-foreground">L {fmtPrice(h.l)} · C {fmtPrice(h.c)}</p>
          <p className={h.c >= h.o ? "text-success" : "text-destructive"}>{(((h.c - h.o) / h.o) * 100).toFixed(2)}%{h.v > 0 ? ` · vol ${compact(h.v)}` : ""}</p>
        </div>)}
    </div>
  );
}
