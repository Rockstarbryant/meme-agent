"use client";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

interface Props {
  open: boolean; onOpenChange: (o: boolean) => void; title: string; description: string; confirmLabel: string;
  destructive?: boolean; phrase?: string; busy?: boolean; extra?: React.ReactNode; onConfirm: () => void | Promise<void>;
}

/** Critical actions require an explicit confirm; the highest-risk ones also require typing an exact phrase. */
export function ConfirmDialog({ open, onOpenChange, title, description, confirmLabel, destructive, phrase, busy, extra, onConfirm }: Props) {
  const [typed, setTyped] = useState("");
  const blocked = busy || (phrase !== undefined && typed !== phrase);
  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) setTyped(""); onOpenChange(o); }}>
      <DialogContent>
        <DialogTitle className="text-2xl font-normal leading-[1.2] tracking-[-0.01em]">{title}</DialogTitle>
        <DialogDescription className="mt-3 text-base leading-[1.7] text-muted-foreground">{description}</DialogDescription>
        {phrase !== undefined && (
          <div className="mt-5 space-y-2">
            <p className="text-sm text-muted-foreground">Type <code className="rounded-sm bg-muted px-1.5 py-0.5 font-mono text-[13px] font-medium text-foreground">{phrase}</code> to continue</p>
            <Input aria-label="confirmation phrase" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
          </div>
        )}
        {extra && <div className="mt-4">{extra}</div>}
        <div className="mt-8 flex flex-col-reverse gap-2 border-t pt-5 sm:flex-row sm:justify-end">
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant={destructive ? "destructive" : "default"} disabled={blocked} onClick={() => void onConfirm()}>{confirmLabel}</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
