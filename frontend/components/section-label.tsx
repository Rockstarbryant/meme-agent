import type { ElementType, ReactNode } from "react";
import { cn } from "@/lib/utils";

/** Section opener: a hairline, a tracked mono label in gold ink, a hairline. Used where a page changes subject. */
export function SectionLabel({ children, as: Tag = "h2", className }: { children: ReactNode; as?: ElementType; className?: string }) {
  return (
    <div className={cn("flex items-center gap-4", className)}>
      <span aria-hidden className="h-px flex-1 bg-border" />
      <Tag className="small-caps text-accent-ink">{children}</Tag>
      <span aria-hidden className="h-px flex-1 bg-border" />
    </div>
  );
}
