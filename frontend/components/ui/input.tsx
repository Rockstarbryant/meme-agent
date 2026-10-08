import * as React from "react";
import { cn } from "@/lib/utils";

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(({ className, type, ...p }, ref) => (
  <input ref={ref} type={type} className={cn(
    "h-11 w-full touch-manipulation rounded-md border border-input bg-transparent px-3.5 text-base transition-all duration-150 ease-out placeholder:text-muted-foreground/70",
    "hover:border-border-hover focus-visible:border-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:opacity-50",
    className)} {...p} />
));
Input.displayName = "Input";

export const Label = ({ className, ...p }: React.LabelHTMLAttributes<HTMLLabelElement>) => (
  <label className={cn("text-sm font-medium tracking-[0.02em]", className)} {...p} />
);
