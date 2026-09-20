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
        <DialogTitle className="text-base font-semibold">{title}</DialogTitle>
        <DialogDescription className="mt-2 text-sm text-muted-foreground">{description}</DialogDescription>
        {phrase !== undefined && (
          <div className="mt-3 space-y-1">
            <p className="text-xs text-muted-foreground">Type <code className="font-mono font-semibold">{phrase}</code> to continue</p>
            <Input aria-label="confirmation phrase" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
          </div>
        )}
        {extra && <div className="mt-3">{extra}</div>}
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant={destructive ? "destructive" : "default"} disabled={blocked} onClick={() => void onConfirm()}>{confirmLabel}</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
