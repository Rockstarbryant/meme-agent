import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva("inline-flex items-center rounded-sm border px-2 py-0.5 text-xs font-semibold tracking-wide", {
  variants: {
    variant: {
      default: "bg-muted text-foreground",
      success: "border-success text-success",
      destructive: "border-destructive text-destructive",
      warning: "border-warning text-warning",
      solidDestructive: "border-transparent bg-destructive text-destructive-foreground",
      solidWarning: "border-transparent bg-warning text-background",
      solidPrimary: "border-transparent bg-primary text-primary-foreground",
    },
  },
  defaultVariants: { variant: "default" },
});

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}
export const Badge = ({ className, variant, ...p }: BadgeProps) => <span className={cn(badgeVariants({ variant }), className)} {...p} />;
