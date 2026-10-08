import * as React from "react";
import { cn } from "@/lib/utils";

export interface CardProps extends React.HTMLAttributes<HTMLDivElement> {
  /** 2px gold rule along the top edge: marks the one card that matters on a page. */
  accentTop?: boolean;
  /** More depth for important content. */
  elevated?: boolean;
  /** Faint gold tint (6%) for highlighted content; usually combined with accentTop. */
  featured?: boolean;
  /** Shadow, border and tint respond to the pointer. No lift: restraint. */
  hoverEffect?: boolean;
}

export const Card = ({ className, accentTop, elevated, featured, hoverEffect, ...p }: CardProps) => (
  <div
    className={cn(
      "relative rounded-lg border bg-card text-card-foreground shadow-sm transition-[box-shadow,border-color,background-color] duration-200 ease-out",
      elevated && "shadow-md",
      featured && "bg-accent/[0.06]",
      accentTop && "before:pointer-events-none before:absolute before:inset-x-0 before:-top-px before:h-0.5 before:rounded-t-lg before:bg-accent",
      hoverEffect && "hover:border-border-hover hover:bg-muted/30 hover:shadow-md",
      className,
    )}
    {...p}
  />
);
export const CardHeader = ({ className, ...p }: React.HTMLAttributes<HTMLDivElement>) => (
  <div className={cn("flex flex-col gap-1.5 p-5 pb-2 sm:p-6 sm:pb-3", className)} {...p} />
);
export const CardTitle = ({ className, ...p }: React.HTMLAttributes<HTMLHeadingElement>) => (
  <h3 className={cn("font-serif text-xl font-semibold leading-[1.3]", className)} {...p} />
);
export const CardContent = ({ className, ...p }: React.HTMLAttributes<HTMLDivElement>) => (
  <div className={cn("p-5 pt-2 sm:p-6 sm:pt-3", className)} {...p} />
);
