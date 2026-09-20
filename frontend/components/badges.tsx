import { Badge } from "@/components/ui/badge";
import type { Action, Mode } from "@/types/api";

const ACTION_VARIANT = { BUY: "success", WATCH: "warning", REJECT: "destructive", HOLD: "default", SELL: "warning" } as const;
export const ActionBadge = ({ action }: { action: Action }) => <Badge variant={ACTION_VARIANT[action]}>{action}</Badge>;

/** PAPER vs LIVE must never be ambiguous. */
export const ModeBadge = ({ mode }: { mode: Mode }) =>
  mode === "LIVE" ? <Badge variant="solidDestructive">LIVE MODE</Badge> : <Badge variant="solidPrimary">PAPER MODE</Badge>;

/** Demo data must never be mistaken for live blockchain data. */
export const DataLabel = ({ label }: { label: string }) =>
  label.toUpperCase().includes("DEMO") ? <Badge variant="solidWarning">DEMO DATA</Badge> : <Badge variant="success">LIVE DATA</Badge>;

export const SimulatedLabel = ({ simulated }: { simulated: boolean }) =>
  simulated ? <Badge variant="warning">PAPER (SIMULATED)</Badge> : <Badge variant="destructive">LIVE</Badge>;
