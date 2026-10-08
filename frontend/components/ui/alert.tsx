import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

// Body text stays ink for readability; severity is carried by the left rule, the tint and the wording itself.
const alertVariants = cva("rounded-md border border-l-[3px] p-4 text-sm leading-relaxed text-foreground", {
  variants: {
    variant: {
      default: "border-border border-l-accent bg-muted/60",
      destructive: "border-destructive/30 border-l-destructive bg-destructive/[0.06]",
      warning: "border-warning/30 border-l-warning bg-warning/[0.06]",
      success: "border-success/30 border-l-success bg-success/[0.06]",
    },
  },
  defaultVariants: { variant: "default" },
});
export interface AlertProps extends React.HTMLAttributes<HTMLDivElement>, VariantProps<typeof alertVariants> {}
export const Alert = ({ className, variant, ...p }: AlertProps) => <div role="alert" className={cn(alertVariants({ variant }), className)} {...p} />;
