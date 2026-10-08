"use client";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { AlertTriangle, CheckCircle2, Info, Loader2, X, XCircle } from "lucide-react";
import { cn } from "@/lib/utils";

export type ToastKind = "success" | "error" | "warning" | "info" | "loading";
export interface ToastInput {
  /** Re-using an id UPDATES that toast in place (e.g. "Order placed" -> "Position opened"). */
  id?: string;
  kind?: ToastKind;
  title: string;
  description?: string;
  href?: string;
  linkLabel?: string;
  /** 0 keeps it until dismissed / updated. Defaults: 6s, 10s for errors, loading waits for its result. */
  durationMs?: number;
}
interface ToastItem extends Required<Pick<ToastInput, "id" | "kind" | "title">> { description?: string; href?: string; linkLabel?: string; durationMs: number; seq: number }

export interface ToastApi {
  toast: (t: ToastInput) => string;
  dismiss: (id: string) => void;
  /** Automatic (event-driven) toasts stay quiet for a while: a flow the user just started reports its own result. */
  suppressAuto: (ms: number) => void;
  autoSuppressed: () => boolean;
}
const noop: ToastApi = { toast: () => "", dismiss: () => undefined, suppressAuto: () => undefined, autoSuppressed: () => false };
const Ctx = createContext<ToastApi>(noop);
export const useToast = () => useContext(Ctx);

const ICON = { success: CheckCircle2, error: XCircle, warning: AlertTriangle, info: Info, loading: Loader2 } as const;
// A coloured rule on the left edge and a coloured icon carry the kind; the words stay in ink.
const TONE: Record<ToastKind, string> = {
  success: "border-l-success", error: "border-l-destructive", warning: "border-l-warning",
  info: "border-l-accent", loading: "border-l-accent",
};
const ICON_TONE: Record<ToastKind, string> = {
  success: "text-success", error: "text-destructive", warning: "text-warning", info: "text-accent-ink", loading: "text-accent-ink",
};
const MAX_VISIBLE = 4;
const defaultDuration = (k: ToastKind) => (k === "error" ? 10000 : k === "loading" ? 0 : 6000);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const seq = useRef(0);
  const quietUntil = useRef(0);

  const dismiss = useCallback((id: string) => {
    const t = timers.current.get(id);
    if (t) clearTimeout(t);
    timers.current.delete(id);
    setItems((prev) => prev.filter((i) => i.id !== id));
  }, []);

  const toast = useCallback((input: ToastInput) => {
    const id = input.id ?? `t${++seq.current}`;
    const kind = input.kind ?? "info";
    const durationMs = input.durationMs ?? defaultDuration(kind);
    const item: ToastItem = { id, kind, title: input.title, description: input.description, href: input.href, linkLabel: input.linkLabel, durationMs, seq: ++seq.current };
    setItems((prev) => [...prev.filter((i) => i.id !== id), item].slice(-MAX_VISIBLE));
    const old = timers.current.get(id);
    if (old) clearTimeout(old);
    // A "loading" toast is a promise of a result; it never silently vanishes before 2 minutes.
    const ms = kind === "loading" && durationMs === 0 ? 120000 : durationMs;
    if (ms > 0) timers.current.set(id, setTimeout(() => dismiss(id), ms));
    return id;
  }, [dismiss]);

  useEffect(() => () => { timers.current.forEach((t) => clearTimeout(t)); }, []);
  const api = useMemo<ToastApi>(() => ({
    toast, dismiss,
    suppressAuto: (ms) => { quietUntil.current = Math.max(quietUntil.current, Date.now() + ms); },
    autoSuppressed: () => Date.now() < quietUntil.current,
  }), [toast, dismiss]);

  return (
    <Ctx.Provider value={api}>
      {children}
      <div aria-live="polite" aria-relevant="additions text" className="pointer-events-none fixed inset-x-0 bottom-20 z-[60] flex flex-col items-center gap-2 px-3 md:bottom-4 md:items-end md:px-4">
        {items.map((t) => {
          const Icon = ICON[t.kind];
          return (
            <div key={t.id} role={t.kind === "error" ? "alert" : "status"}
              className={cn("pointer-events-auto flex w-full max-w-sm animate-[fade-in_0.25s_ease-out_both] items-start gap-3 rounded-lg border border-l-[3px] bg-card p-4 text-card-foreground shadow-lg", TONE[t.kind])}>
              <Icon className={cn("mt-0.5 h-4 w-4 shrink-0", ICON_TONE[t.kind], t.kind === "loading" && "animate-spin")} aria-hidden />
              <div className="min-w-0 flex-1 text-sm">
                <p className="font-medium leading-snug">{t.title}</p>
                {t.description && <p className="mt-1 break-words text-xs leading-relaxed text-muted-foreground">{t.description}</p>}
                {t.href && <Link href={t.href} className="mt-1.5 inline-block text-xs underline">{t.linkLabel ?? "View"}</Link>}
              </div>
              <button type="button" aria-label="Dismiss notification" onClick={() => dismiss(t.id)} className="-m-1 grid h-9 w-9 shrink-0 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"><X className="h-3.5 w-3.5" /></button>
            </div>
          );
        })}
      </div>
    </Ctx.Provider>
  );
}
