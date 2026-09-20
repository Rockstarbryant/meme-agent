import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const alertVariants = cva("rounded-md border p-3 text-sm", {
  variants: {
    variant: {
      default: "bg-muted",
      destructive: "border-destructive text-destructive",
      warning: "border-warning text-warning",
      success: "border-success text-success",
    },
  },
  defaultVariants: { variant: "default" },
});
export interface AlertProps extends React.HTMLAttributes<HTMLDivElement>, VariantProps<typeof alertVariants> {}
export const Alert = ({ className, variant, ...p }: AlertProps) => <div role="alert" className={cn(alertVariants({ variant }), className)} {...p} />;
