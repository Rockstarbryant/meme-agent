# Frontend (Next.js 16, TypeScript, Tailwind 3, shadcn-style components, Lucide)
Mobile-first: bottom tab bar on phones, sidebar from `md`. No emoji icons. System fonts only (no network font fetch).

## Pages
Dashboard `/`, Opportunities `/opportunities`, Token detail `/tokens/[key]`, Decision "why" `/decisions/[id]`, Positions,
Strategies, Agent control `/agent`, Activity, Settings, Wallet, Login. Every page shows a persistent banner: **PAPER MODE / LIVE MODE**,
**DEMO DATA** (when the data source is synthetic), agent state, live-stream status and a red **EMERGENCY STOP** banner.

## Local Runner in the UI
The Agent page embeds the **Runner panel**: generate a single-use pairing code (with the exact `python -m runner pair ... / run` commands), see whether the runner
is online, its config sync (`applied vX of vY`), wallet provider, the reasons LIVE is unavailable, its local ceilings, dead-man-switch suspensions and last error, and
revoke it. The banner shows NO RUNNER / RUNNER ONLINE / RUNNER OFFLINE on every page. Agent state distinguishes what you **requested** from what the runner **reports**
(RUNNING, PAUSED, STOPPED, OFFLINE, LIVE_BLOCKED). Manual close is a *request* delivered to the runner. Strategies can be enabled/disabled.

## Safety behaviour in the UI
- LIVE needs the exact phrase `ENABLE LIVE TRADING`; the server re-checks everything and its blockers are shown inside the dialog.
- Emergency stop, closing positions, revoking authorization and LIVE-mode limit changes all require a confirmation dialog.
- Wallet page states all three capabilities (autonomous delegated / per-trade signing / paper only) and why autonomous is unavailable.
- Unknowns are shown as unknown ("creator unverified", "missing data"), never as safe. Nothing is fabricated: empty states say so.

## Auth and live updates
- JWT kept in `sessionStorage` (cleared when the tab closes) and sent as a Bearer header; it is never put in a URL.
  Trade-off: an XSS bug could read it. No CSP is configured yet; add one before production.
- SSE: the browser trades its token for a 60-second single-use ticket (`POST /stream/ticket`), then opens `EventSource` on
  `/stream?ticket=...`. Matching events trigger debounced refetches. Reconnects with a fresh ticket.

## Wallet (browser, EIP-1193)
Connect = request accounts -> switch/add Arc using the documented parameters (native USDC, 18 decimals) -> sign a one-time challenge
(`personal_sign`) -> backend verifies the signature. No key is ever requested. Queued signing requests are sent with `eth_sendTransaction`.

## Config and commands
`NEXT_PUBLIC_API_URL` (build-time, public; never put secrets in `NEXT_PUBLIC_*`). Add the frontend origin to backend `CORS_ORIGINS`.
`npm run dev | build | lint | typecheck | test`.

## Verified
82 Vitest tests (jsdom), ESLint, `tsc`, production build, and a live contract check of every endpoint against the TypeScript types.
NOT verified: behaviour in a real browser with a real wallet extension (no browser available in the build sandbox).
