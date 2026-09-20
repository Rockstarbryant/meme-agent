import { Loader2 } from "lucide-react";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import type { ApiError } from "@/lib/api";

export const Loading = ({ label = "Loading…" }: { label?: string }) => (
  <div role="status" className="flex items-center gap-2 p-4 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" />{label}</div>
);

export function ErrorState({ error, onRetry }: { error: ApiError; onRetry?: () => void }) {
  return (
    <Alert variant="destructive">
      <p className="font-medium">{error.message}</p>
      {error.blockers.length > 0 && <ul className="mt-2 list-disc pl-5">{error.blockers.map((b) => <li key={b}>{b}</li>)}</ul>}
      {onRetry && <Button size="sm" variant="outline" className="mt-2" onClick={onRetry}>Retry</Button>}
    </Alert>
  );
}

export const Empty = ({ children }: { children: React.ReactNode }) => (
  <p className="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground">{children}</p>
);

export const Stat = ({ label, value, sub, valueClass }: { label: string; value: React.ReactNode; sub?: React.ReactNode; valueClass?: string }) => (
  <div><p className="text-xs text-muted-foreground">{label}</p><p className={`text-lg font-semibold tabular-nums ${valueClass ?? ""}`}>{value}</p>{sub && <p className="text-xs text-muted-foreground">{sub}</p>}</div>
);
