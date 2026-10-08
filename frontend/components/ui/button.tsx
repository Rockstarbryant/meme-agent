import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

/**
 * Primary is burnished gold with dark text. Outline is an ink hairline. Ghost is quiet text that gains a gold underline.
 * Any button with aria-pressed (filters, ranges, sort) shows its selected state as an ink fill, so gold stays reserved for actions.
 */
const buttonVariants = cva(
  [
    "inline-flex touch-manipulation items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium tracking-[0.02em]",
    "transition-all duration-200 ease-out motion-reduce:transform-none",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background",
    "disabled:pointer-events-none disabled:opacity-50",
    "aria-[pressed=true]:translate-y-0 aria-[pressed=true]:border-foreground aria-[pressed=true]:bg-foreground aria-[pressed=true]:text-background aria-[pressed=true]:shadow-none",
    "aria-[pressed=true]:hover:translate-y-0 aria-[pressed=true]:hover:border-foreground aria-[pressed=true]:hover:bg-foreground aria-[pressed=true]:hover:text-background",
  ].join(" "),
  {
    variants: {
      variant: {
        default: "border border-transparent bg-primary text-primary-foreground shadow-sm hover:-translate-y-0.5 hover:bg-accent-secondary hover:shadow-gold active:translate-y-0",
        outline: "border border-foreground bg-transparent text-foreground hover:border-accent hover:bg-muted hover:text-accent-ink",
        destructive: "border border-transparent bg-destructive text-destructive-foreground shadow-sm hover:bg-destructive/90",
        ghost: "border border-transparent text-muted-foreground decoration-accent underline-offset-4 hover:text-foreground hover:underline",
      },
      size: {
        default: "h-10 px-5 max-md:min-h-[44px]",
        sm: "h-9 px-3.5 text-[13px] max-md:min-h-[44px]",
        lg: "h-12 px-7 text-base",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> { asChild?: boolean }

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(({ className, variant, size, asChild, ...props }, ref) => {
  const Comp = asChild ? Slot : "button";
  return <Comp ref={ref} className={cn(buttonVariants({ variant, size }), className)} {...props} />;
});
Button.displayName = "Button";
