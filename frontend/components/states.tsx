import { Loader2 } from "lucide-react";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import type { ApiError } from "@/lib/api";

export const Loading = ({ label = "Loading…" }: { label?: string }) => (
  <div role="status" className="flex items-center gap-2 p-4 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin text-accent" />{label}</div>
);

export function ErrorState({ error, onRetry }: { error: ApiError; onRetry?: () => void }) {
  return (
    <Alert variant="destructive">
      <p className="font-medium">{error.message}</p>
      {error.blockers.length > 0 && <ul className="mt-2 list-disc pl-5">{error.blockers.map((b) => <li key={b}>{b}</li>)}</ul>}
      {onRetry && <Button size="sm" variant="outline" className="mt-3" onClick={onRetry}>Retry</Button>}
    </Alert>
  );
}

export const Empty = ({ children }: { children: React.ReactNode }) => (
  <p className="rounded-lg border border-dashed border-border-hover bg-card/40 px-6 py-10 text-center text-sm leading-relaxed text-muted-foreground">{children}</p>
);

export const Stat = ({ label, value, sub, valueClass }: { label: string; value: React.ReactNode; sub?: React.ReactNode; valueClass?: string }) => (
  <div><p className="text-xs tracking-[0.03em] text-muted-foreground">{label}</p><p className={`display-num mt-1 break-words text-xl md:text-2xl ${valueClass ?? ""}`}>{value}</p>{sub && <p className="mt-0.5 text-xs text-muted-foreground">{sub}</p>}</div>
);
