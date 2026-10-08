"use client";
import { useState } from "react";
import { Bell, Globe, Send } from "lucide-react";
import { ErrorState, Loading } from "@/components/states";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { useApi } from "@/hooks/use-api";
import { api, toApiError, type ApiError } from "@/lib/api";
import { ago } from "@/lib/format";
import { useNotificationPrefs, type NotificationPrefs } from "@/lib/prefs";

interface CatalogItem { id: string; group: string; label: string; description: string }
interface Delivery { at: string; ok: boolean; http_status?: number; error?: string; event?: string }
interface NotificationsConfig { enabled: boolean; webhook_url: string | null; events: string[]; available_events: string[]; event_catalog?: CatalogItem[]; last_delivery?: Delivery | null }
interface TestResult { delivered: boolean; http_status: number | null; elapsed_ms: number; message: string; sent: unknown }

const IN_APP: { key: keyof NotificationPrefs; label: string; detail: string }[] = [
  { key: "agentState", label: "Agent state", detail: "Started, paused, stopped, or the runner going offline." },
  { key: "orders", label: "Orders and entries", detail: "Order placed, position opened, order failed." },
  { key: "exits", label: "Exits", detail: "Take-profit, stop-loss, trailing stop and positions closing, with the reason and result." },
  { key: "safety", label: "Safety", detail: "Emergency stop changes and agent errors." },
];

function InAppCard() {
  const [prefs, update] = useNotificationPrefs();
  return (
    <Card><CardHeader><div className="flex items-center gap-2"><Bell className="h-4 w-4" aria-hidden /><CardTitle>Pop-up notifications in this browser</CardTitle></div></CardHeader>
      <CardContent className="space-y-3 text-sm">
        <p className="text-muted-foreground">The small messages that appear on screen while you use the app (for example &quot;Position opened&quot;). They only show while a page is open, and this choice is remembered in this browser only.</p>
        {IN_APP.map((o) => (
          <label key={o.key} className="flex min-h-[44px] cursor-pointer items-start gap-3 rounded-md border p-3.5 transition-colors duration-200 hover:border-border-hover hover:bg-muted/40">
            <input type="checkbox" className="mt-1 h-4 w-4" checked={prefs[o.key]} onChange={(e) => update({ [o.key]: e.target.checked })} />
            <span><span className="font-medium">{o.label}</span><span className="block text-xs text-muted-foreground">{o.detail}</span></span>
          </label>))}
        <p className="text-xs text-muted-foreground">Actions you take yourself (pressing Start, Retry AI, Buy anyway) always show their result.</p>
      </CardContent></Card>
  );
}

function WebhookCard() {
  const n = useApi<NotificationsConfig>("/notifications");
  const [enabled, setEnabled] = useState(false);
  const [url, setUrl] = useState("");
  const [events, setEvents] = useState<string[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [test, setTest] = useState<TestResult | null>(null);
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  if (n.data && !loaded) { setEnabled(n.data.enabled); setUrl(n.data.webhook_url ?? ""); setEvents(n.data.events); setLoaded(true); }

  if (n.loading && !n.data) return <Card><CardHeader><CardTitle>Send events to another app</CardTitle></CardHeader><CardContent><Loading /></CardContent></Card>;
  const catalog: CatalogItem[] = n.data?.event_catalog ?? (n.data?.available_events ?? []).map((id) => ({ id, group: "Events", label: id, description: "" }));
  const groups = [...new Set(catalog.map((c) => c.group))];
  const toggle = (id: string) => setEvents((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]));
  const all = events.length === 0;
  const d = n.data?.last_delivery;

  async function save() {
    setBusy("save"); setError(null); setNotice(null);
    try {
      await api("/notifications", { method: "PUT", body: { enabled, webhook_url: url.trim() || null, events } });
      await n.reload(); setNotice("Saved.");
    } catch (e) { setError(toApiError(e)); } finally { setBusy(null); }
  }
  async function sendTest() {
    setBusy("test"); setError(null); setTest(null);
    try { setTest(await api<TestResult>("/notifications/test", { method: "POST", body: { webhook_url: url.trim() || null } })); }
    catch (e) { setError(toApiError(e)); } finally { setBusy(null); }
  }

  return (
    <Card><CardHeader><div className="flex items-center gap-2"><Globe className="h-4 w-4" aria-hidden /><CardTitle>Send events to another app (webhook)</CardTitle></div></CardHeader>
      <CardContent className="space-y-4 text-sm">
        <div className="space-y-1">
          <p className="text-muted-foreground">A webhook sends a message to a web address you own whenever something happens, so you can get it in Slack, Discord, Telegram (through a bridge such as Zapier or n8n), or your own server, even when this app is closed.</p>
          <ol className="list-decimal space-y-0.5 pl-5 text-xs text-muted-foreground">
            <li>Create an incoming-webhook URL in the app you want the messages in.</li>
            <li>Paste it below, choose the events, press <strong>Send a test</strong>, then <strong>Save</strong>.</li>
            <li>Each event arrives as a JSON message: <code>{"{ type, at, payload }"}</code>. Delivery is one attempt with a 5-second limit, with no retries.</li>
          </ol>
        </div>

        {error && <ErrorState error={error} />}
        {notice && <Alert variant="success">{notice}</Alert>}

        <label className="flex items-center gap-2 font-medium"><input type="checkbox" className="h-4 w-4" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />Send events to this address</label>
        <div className="space-y-1"><Label htmlFor="wh-url">Webhook URL</Label>
          <Input id="wh-url" placeholder="https://hooks.example.com/your-endpoint" value={url} onChange={(e) => { setUrl(e.target.value); setTest(null); }} autoComplete="off" spellCheck={false} />
          <p className="text-xs text-muted-foreground">Public https addresses only. Addresses on a private or internal network are refused.</p></div>

        <div className="space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2"><p className="font-medium">Which events</p>
            <Badge variant={all ? "warning" : "success"}>{all ? "ALL events" : `${events.length} selected`}</Badge></div>
          {all && <Alert variant="warning">Nothing is selected, so <strong>every</strong> event is sent, including each decision the agent records (dozens per hour). Pick only the events you want.</Alert>}
          {groups.map((g) => (
            <div key={g} className="space-y-1"><p className="small-caps text-[11px] text-muted-foreground">{g}</p>
              {catalog.filter((c) => c.group === g).map((c) => (
                <label key={c.id} className="flex cursor-pointer items-start gap-3 rounded-md border p-3 transition-colors duration-200 hover:border-border-hover hover:bg-muted/40">
                  <input type="checkbox" className="mt-1 h-4 w-4" checked={events.includes(c.id)} onChange={() => toggle(c.id)} />
                  <span><span className="font-medium">{c.label}</span> <code className="text-[11px] text-muted-foreground">{c.id}</code>{c.description && <span className="block text-xs text-muted-foreground">{c.description}</span>}</span>
                </label>))}
            </div>))}
          <div className="flex gap-2 text-xs"><button type="button" className="underline" onClick={() => setEvents(catalog.map((c) => c.id))}>Select all</button><button type="button" className="underline" onClick={() => setEvents([])}>Clear</button></div>
        </div>

        <div className="flex flex-wrap gap-2">
          <Button variant="outline" disabled={busy !== null || !url.trim()} onClick={() => void sendTest()}><Send className="mr-1.5 h-4 w-4" aria-hidden />{busy === "test" ? "Sending…" : "Send a test"}</Button>
          <Button disabled={busy !== null} onClick={() => void save()}>{busy === "save" ? "Saving…" : "Save"}</Button>
        </div>
        {test && <Alert variant={test.delivered ? "success" : "warning"}>{test.message}{test.http_status != null ? ` (HTTP ${test.http_status}, ${test.elapsed_ms} ms)` : ""}</Alert>}

        <div className="rounded-md border p-3 text-xs">
          <p className="font-medium">Last real delivery</p>
          {d ? <p className={d.ok ? "text-success" : "text-destructive"}>{d.ok ? "Worked" : "Failed"}: {d.event ?? "event"} {ago(d.at)}{d.http_status ? ` (HTTP ${d.http_status})` : ""}{d.error ? `. ${d.error}` : ""}</p>
            : <p className="text-muted-foreground">Nothing has been sent yet. It is updated when an event is delivered.</p>}
        </div>
      </CardContent></Card>
  );
}

export function NotificationSettings() {
  return <div className="space-y-4"><InAppCard /><WebhookCard /></div>;
}
