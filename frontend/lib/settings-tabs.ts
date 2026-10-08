export const SETTINGS_TABS = [
  { id: "general", label: "General", hint: "Mode, chain, launchpads, market data" },
  { id: "strategy", label: "Strategy", hint: "Weights, thresholds and exit rules" },
  { id: "risk", label: "Risk and limits", hint: "Trade size, loss limits, blacklists" },
  { id: "wallet", label: "Wallet and policy", hint: "Spending policy, signing, wallet connection" },
  { id: "runner", label: "Runner and AI", hint: "Cloud or local runner, AI providers" },
  { id: "notifications", label: "Notifications", hint: "Pop-ups and webhooks" },
  { id: "safety", label: "Safety", hint: "Emergency stop" },
] as const;
export type SettingsTab = (typeof SETTINGS_TABS)[number]["id"];

/** Reads ?tab= safely: anything unknown falls back to General. */
export function parseTab(value: string | null | undefined): SettingsTab {
  return (SETTINGS_TABS.find((t) => t.id === value)?.id ?? "general") as SettingsTab;
}
