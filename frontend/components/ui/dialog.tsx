"use client";
import * as React from "react";
import * as D from "@radix-ui/react-dialog";
import { cn } from "@/lib/utils";

export const Dialog = D.Root;
export const DialogTitle = D.Title;
export const DialogDescription = D.Description;
export const DialogClose = D.Close;

export function DialogContent({ className, children, ...p }: React.ComponentPropsWithoutRef<typeof D.Content>) {
  return (
    <D.Portal>
      <D.Overlay className="fixed inset-0 z-50 bg-foreground/40 backdrop-blur-[2px] data-[state=open]:animate-[fade-in_0.2s_ease-out_both]" />
      <D.Content className={cn(
        "fixed left-1/2 top-1/2 z-50 max-h-[90vh] w-[92vw] max-w-md -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-lg border bg-card p-6 shadow-lg data-[state=open]:animate-[fade-in_0.2s_ease-out_both] sm:p-8",
        "before:pointer-events-none before:absolute before:inset-x-0 before:-top-px before:h-0.5 before:rounded-t-lg before:bg-accent",
        className)} {...p}>
        {children}
      </D.Content>
    </D.Portal>
  );
}
