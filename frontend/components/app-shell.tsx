"use client";
import { useEffect, useState, type ReactNode } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { LogOut, MoreHorizontal, X } from "lucide-react";
import { NotificationWatcher } from "@/components/notification-watcher";
import { StatusBanner } from "@/components/status-banner";
import { Button } from "@/components/ui/button";
import { Loading } from "@/components/states";
import { useAuth } from "@/lib/auth";
import { ADMIN_GROUP, MOBILE_MORE, MOBILE_PRIMARY, NAV_GROUPS, isActive } from "@/lib/nav";
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

  const groups = user.is_admin ? [...NAV_GROUPS, ADMIN_GROUP] : NAV_GROUPS;
  const moreItems = user.is_admin ? [...MOBILE_MORE, ...ADMIN_GROUP.items] : MOBILE_MORE;
  const moreActive = moreItems.some((i) => isActive(path, i.href));
  return (
    <div className="min-h-screen md:flex">
      <NotificationWatcher />
      <aside className="hidden w-60 shrink-0 border-r bg-card/50 px-4 py-6 md:sticky md:top-0 md:flex md:h-screen md:flex-col md:self-start md:overflow-y-auto">
        <Link href="/" className="mb-10 block px-2">
          <span className="block font-serif text-[1.75rem] leading-none tracking-[-0.01em]">Arc Agent</span>
          <span className="mt-3 flex items-center gap-2"><span aria-hidden className="h-px w-6 bg-accent" /><span className="font-mono text-[11px] tracking-[0.12em] text-muted-foreground">autonomous trading</span></span>
        </Link>
        <nav aria-label="Main" className="flex-1 space-y-7">
          {groups.map((g) => (
            <div key={g.label}>
              <p className="small-caps mb-2 px-2 text-muted-foreground">{g.label}</p>
              <div className="space-y-0.5">
                {g.items.map(({ href, label, icon: Icon }) => (
                  <Link key={href} href={href} aria-current={isActive(path, href) ? "page" : undefined}
                    className={cn("flex min-h-[40px] items-center gap-3 rounded-l-none rounded-r-md border-l-2 border-transparent px-3 py-2 text-sm font-medium tracking-[0.05em] text-muted-foreground transition-colors duration-200 ease-out hover:bg-muted/70 hover:text-foreground",
                      isActive(path, href) && "border-accent bg-muted text-foreground")}>
                    <Icon className="h-4 w-4" aria-hidden />{label}
                  </Link>))}
              </div>
            </div>))}
        </nav>
        <p className="mt-6 truncate border-t px-2 pt-4 text-xs tracking-[0.03em] text-muted-foreground">{user.email}</p>
      </aside>

      <div className="min-w-0 flex-1">
        <header className="sticky top-0 z-30 border-b bg-background/90 px-4 py-3 backdrop-blur md:px-8">
          <div className="flex items-start justify-between gap-3">
            <StatusBanner />
            <div className="flex shrink-0 items-center gap-2">
              <span className="hidden max-w-40 truncate text-xs tracking-[0.03em] text-muted-foreground sm:inline md:hidden">{user.email}</span>
              <Button size="sm" variant="outline" aria-label="Log out" onClick={() => void logout().then(() => router.replace("/login"))}><LogOut className="h-4 w-4" /></Button>
            </div>
          </div>
        </header>
        <main className="mx-auto max-w-6xl space-y-8 px-4 py-8 pb-28 md:space-y-10 md:px-8 md:py-12 md:pb-16">{children}</main>
      </div>

      {more && <button type="button" aria-label="Close menu" className="fixed inset-0 z-40 bg-foreground/40 md:hidden" onClick={() => setMore(false)} />}
      {more && (
        <div id="more-sheet" role="dialog" aria-label="More pages" className="fixed inset-x-0 bottom-16 z-50 rounded-t-lg border-t bg-background p-4 shadow-lg md:hidden">
          <div className="mb-3 flex items-center justify-between px-1"><p className="font-serif text-lg">More</p>
            <button type="button" aria-label="Close menu" className="grid h-11 w-11 place-items-center rounded-md transition-colors hover:bg-muted" onClick={() => setMore(false)}><X className="h-4 w-4" /></button></div>
          <div className="grid grid-cols-3 gap-2">
            {moreItems.map(({ href, label, icon: Icon }) => (
              <Link key={href} href={href} aria-current={isActive(path, href) ? "page" : undefined}
                className={cn("flex min-h-[72px] flex-col items-center justify-center gap-1.5 rounded-md border p-3 text-xs tracking-[0.03em] transition-colors duration-200", isActive(path, href) ? "border-accent bg-accent/10 font-semibold" : "hover:border-border-hover hover:bg-muted")}>
                <Icon className="h-5 w-5" aria-hidden />{label}
              </Link>))}
          </div>
        </div>)}
      <nav aria-label="Mobile" className="fixed inset-x-0 bottom-0 z-50 flex border-t bg-background/95 pb-[env(safe-area-inset-bottom)] backdrop-blur md:hidden">
        {MOBILE_PRIMARY.map(({ href, label, icon: Icon }) => (
          <Link key={href} href={href} aria-current={isActive(path, href) ? "page" : undefined}
            className={cn("relative flex min-h-[56px] min-w-0 flex-1 touch-manipulation flex-col items-center justify-center gap-0.5 px-1 py-2 text-[11px] tracking-[0.04em] transition-colors duration-200", isActive(path, href) ? "font-semibold text-foreground before:absolute before:inset-x-4 before:top-0 before:h-0.5 before:bg-accent" : "text-muted-foreground")}>
            <Icon className="h-5 w-5" aria-hidden /><span className="truncate">{label}</span>
          </Link>))}
        <button type="button" aria-expanded={more} aria-controls="more-sheet" onClick={() => setMore(!more)}
          className={cn("relative flex min-h-[56px] min-w-0 flex-1 touch-manipulation flex-col items-center justify-center gap-0.5 px-1 py-2 text-[11px] tracking-[0.04em] transition-colors duration-200", more || moreActive ? "font-semibold text-foreground before:absolute before:inset-x-4 before:top-0 before:h-0.5 before:bg-accent" : "text-muted-foreground")}>
          <MoreHorizontal className="h-5 w-5" aria-hidden />More
        </button>
      </nav>
    </div>
  );
}
