/** Dependency-free SVG line chart. Renders exactly the points it is given; never fabricates data. */
export function PriceChart({ points, height = 140 }: { points: { at: string; price: number | null }[]; height?: number }) {
  const pts = points.filter((p): p is { at: string; price: number } => p.price != null);
  if (pts.length < 2) return <p className="p-4 text-center text-sm text-muted-foreground">Not enough price history yet.</p>;
  const w = 600;
  const min = Math.min(...pts.map((p) => p.price)), max = Math.max(...pts.map((p) => p.price));
  const span = max - min || 1;
  const path = pts.map((p, i) => `${i ? "L" : "M"}${(i / (pts.length - 1)) * w},${height - 8 - ((p.price - min) / span) * (height - 16)}`).join(" ");
  const up = pts[pts.length - 1].price >= pts[0].price;
  return (
    <svg role="img" aria-label="price chart" viewBox={`0 0 ${w} ${height}`} className="w-full" preserveAspectRatio="none">
      <path d={path} fill="none" strokeWidth={2} vectorEffect="non-scaling-stroke" className={up ? "stroke-success" : "stroke-destructive"} />
    </svg>
  );
}
