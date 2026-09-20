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
      <D.Overlay className="fixed inset-0 z-50 bg-black/60" />
      <D.Content className={cn("fixed left-1/2 top-1/2 z-50 w-[92vw] max-w-md -translate-x-1/2 -translate-y-1/2 rounded-lg border bg-card p-5 shadow-lg", className)} {...p}>
        {children}
      </D.Content>
    </D.Portal>
  );
}
