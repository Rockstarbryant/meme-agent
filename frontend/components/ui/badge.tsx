import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva("inline-flex items-center rounded-sm border px-2 py-0.5 font-mono text-[11px] font-medium leading-5 tracking-[0.08em]", {
  variants: {
    variant: {
      default: "border-border bg-muted text-foreground",
      success: "border-success/40 bg-success/[0.07] text-success",
      destructive: "border-destructive/40 bg-destructive/[0.06] text-destructive",
      warning: "border-warning/40 bg-warning/[0.07] text-warning",
      solidDestructive: "border-transparent bg-destructive text-destructive-foreground",
      solidWarning: "border-transparent bg-warning text-background",
      solidPrimary: "border-transparent bg-primary text-primary-foreground",
    },
  },
  defaultVariants: { variant: "default" },
});

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}
export const Badge = ({ className, variant, ...p }: BadgeProps) => <span className={cn(badgeVariants({ variant }), className)} {...p} />;
