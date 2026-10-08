"use client";
import { useEffect, useState, type ReactNode } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Bot, LogOut, MoreHorizontal, X } from "lucide-react";
import { NotificationWatcher } from "@/components/notification-watcher";
import { StatusBanner } from "@/components/status-banner";
import { Button } from "@/components/ui/button";
import { Loading } from "@/components/states";
import { useAuth } from "@/lib/auth";
import { MOBILE_MORE, MOBILE_PRIMARY, NAV_GROUPS, isActive } from "@/lib/nav";
import { cn } from "@/lib/utils";

export function AppShell({ children }: { children: ReactNode }) {
  const { user, ready, logout } = useAuth();
  const router = useRouter();
  const path = usePathname();
  // The sheet is open only on the page it was opened on, so navigating closes it without an effect.
  const [moreAt, setMoreAt] = useState<string | null>(null);
  const more = moreAt === path;
  const setMore = (open: boolean) => setMoreAt(open ? path : null);

  useEffect(() => { if (ready && !user) router.replace("/login"); }, [ready, user, router]);
  if (!ready || !user) return <Loading label="Checking session…" />;

  const moreActive = MOBILE_MORE.some((i) => isActive(path, i.href));
  return (
    <div className="min-h-screen md:flex">
      <NotificationWatcher />
      <aside className="hidden w-56 shrink-0 border-r bg-muted/20 p-3 md:flex md:flex-col">
        <Link href="/" className="mb-5 flex items-center gap-2 px-2 pt-1">
          <span className="grid h-8 w-8 place-items-center rounded-lg bg-primary text-primary-foreground"><Bot className="h-4 w-4" aria-hidden /></span>
          <span className="text-sm font-bold leading-tight">Arc Agent<span className="block text-[11px] font-normal text-muted-foreground">autonomous trading</span></span>
        </Link>
        <nav aria-label="Main" className="flex-1 space-y-4">
          {NAV_GROUPS.map((g) => (
            <div key={g.label}>
              <p className="mb-1 px-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">{g.label}</p>
              <div className="space-y-0.5">
                {g.items.map(({ href, label, icon: Icon }) => (
                  <Link key={href} href={href} aria-current={isActive(path, href) ? "page" : undefined}
                    className={cn("flex items-center gap-2.5 rounded-md border-l-2 border-transparent px-2 py-2 text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
                      isActive(path, href) && "border-primary bg-muted font-semibold text-foreground")}>
                    <Icon className="h-4 w-4" aria-hidden />{label}
                  </Link>))}
              </div>
            </div>))}
        </nav>
        <p className="truncate px-2 pt-3 text-[11px] text-muted-foreground">{user.email}</p>
      </aside>

      <div className="min-w-0 flex-1">
        <header className="sticky top-0 z-30 border-b bg-background/95 p-3 backdrop-blur">
          <div className="flex items-start justify-between gap-3">
            <StatusBanner />
            <div className="flex shrink-0 items-center gap-2">
              <span className="hidden max-w-40 truncate text-xs text-muted-foreground sm:inline md:hidden">{user.email}</span>
              <Button size="sm" variant="outline" aria-label="Log out" onClick={() => void logout().then(() => router.replace("/login"))}><LogOut className="h-4 w-4" /></Button>
            </div>
          </div>
        </header>
        <main className="mx-auto max-w-6xl space-y-4 p-3 pb-24 md:p-6 md:pb-6">{children}</main>
      </div>

      {more && <button type="button" aria-label="Close menu" className="fixed inset-0 z-40 bg-black/50 md:hidden" onClick={() => setMore(false)} />}
      {more && (
        <div id="more-sheet" role="dialog" aria-label="More pages" className="fixed inset-x-0 bottom-14 z-50 rounded-t-2xl border-t bg-background p-3 shadow-2xl md:hidden">
          <div className="mb-2 flex items-center justify-between px-1"><p className="text-sm font-semibold">More</p>
            <button type="button" aria-label="Close menu" className="rounded p-1 hover:bg-muted" onClick={() => setMore(false)}><X className="h-4 w-4" /></button></div>
          <div className="grid grid-cols-3 gap-2">
            {MOBILE_MORE.map(({ href, label, icon: Icon }) => (
              <Link key={href} href={href} aria-current={isActive(path, href) ? "page" : undefined}
                className={cn("flex flex-col items-center gap-1 rounded-lg border p-3 text-xs", isActive(path, href) ? "border-primary bg-primary/10 font-semibold" : "hover:bg-muted")}>
                <Icon className="h-5 w-5" aria-hidden />{label}
              </Link>))}
          </div>
        </div>)}
      <nav aria-label="Mobile" className="fixed inset-x-0 bottom-0 z-50 flex border-t bg-background md:hidden">
        {MOBILE_PRIMARY.map(({ href, label, icon: Icon }) => (
          <Link key={href} href={href} aria-current={isActive(path, href) ? "page" : undefined}
            className={cn("flex min-w-0 flex-1 flex-col items-center gap-0.5 px-1 py-2 text-[11px]", isActive(path, href) ? "font-semibold text-primary" : "text-muted-foreground")}>
            <Icon className="h-5 w-5" aria-hidden /><span className="truncate">{label}</span>
          </Link>))}
        <button type="button" aria-expanded={more} aria-controls="more-sheet" onClick={() => setMore(!more)}
          className={cn("flex min-w-0 flex-1 flex-col items-center gap-0.5 px-1 py-2 text-[11px]", more || moreActive ? "font-semibold text-primary" : "text-muted-foreground")}>
          <MoreHorizontal className="h-5 w-5" aria-hidden />More
        </button>
      </nav>
    </div>
  );
}
