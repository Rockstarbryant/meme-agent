"use client";
import { cn } from "@/lib/utils";

export type HistoryMode = "PAPER" | "LIVE";

/** Paper | Live switch for history views. Paper (virtual money) and Live (real money) are never mixed on one list. */
export function ModeTabs({ value, onChange, current, label = "Trading mode" }: { value: HistoryMode; onChange: (m: HistoryMode) => void; current?: HistoryMode; label?: string }) {
  return (
    <div role="tablist" aria-label={label} className="inline-flex rounded-md border bg-muted/50 p-0.5">
      {(["PAPER", "LIVE"] as const).map((m) => (
        <button key={m} type="button" role="tab" aria-selected={value === m} onClick={() => onChange(m)}
          className={cn("min-h-[44px] touch-manipulation rounded-[5px] px-4 py-1.5 text-xs font-semibold tracking-[0.06em] transition-all duration-200 ease-out md:min-h-[36px]",
            value === m ? (m === "LIVE" ? "bg-destructive text-destructive-foreground shadow-sm" : "bg-foreground text-background shadow-sm") : "text-muted-foreground hover:text-foreground")}>
          {m === "PAPER" ? "Paper" : "Live"}{current === m ? <span className="ml-1 font-normal opacity-80">(current)</span> : null}
        </button>))}
    </div>
  );
}
