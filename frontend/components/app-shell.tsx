"use client";
import { useEffect, type ReactNode } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Activity, Bot, Briefcase, LayoutDashboard, LogOut, Radar, Settings, SlidersHorizontal, Wallet } from "lucide-react";
import { StatusBanner } from "@/components/status-banner";
import { Button } from "@/components/ui/button";
import { Loading } from "@/components/states";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/opportunities", label: "Opportunities", icon: Radar },
  { href: "/positions", label: "Positions", icon: Briefcase },
  { href: "/agent", label: "Agent", icon: Bot },
  { href: "/strategies", label: "Strategies", icon: SlidersHorizontal },
  { href: "/activity", label: "Activity", icon: Activity },
  { href: "/wallet", label: "Wallet", icon: Wallet },
  { href: "/settings", label: "Settings", icon: Settings },
];

export function AppShell({ children }: { children: ReactNode }) {
  const { user, ready, logout } = useAuth();
  const router = useRouter();
  const path = usePathname();

  useEffect(() => { if (ready && !user) router.replace("/login"); }, [ready, user, router]);
  if (!ready || !user) return <Loading label="Checking session…" />;

  const active = (href: string) => (href === "/" ? path === "/" : path.startsWith(href));
  return (
    <div className="min-h-screen md:flex">
      <aside className="hidden w-52 shrink-0 border-r p-3 md:block">
        <p className="mb-4 px-2 text-sm font-bold">Arc Agent</p>
        <nav aria-label="Main" className="space-y-1">
          {NAV.map(({ href, label, icon: Icon }) => (
            <Link key={href} href={href} aria-current={active(href) ? "page" : undefined}
              className={cn("flex items-center gap-2 rounded-md px-2 py-2 text-sm hover:bg-muted", active(href) && "bg-muted font-semibold")}>
              <Icon className="h-4 w-4" aria-hidden />{label}
            </Link>
          ))}
        </nav>
      </aside>
      <div className="min-w-0 flex-1">
        <header className="sticky top-0 z-30 border-b bg-background/95 p-3 backdrop-blur">
          <div className="flex items-start justify-between gap-3">
            <StatusBanner />
            <div className="flex shrink-0 items-center gap-2">
              <span className="hidden max-w-40 truncate text-xs text-muted-foreground sm:inline">{user.email}</span>
              <Button size="sm" variant="outline" aria-label="Log out" onClick={() => void logout().then(() => router.replace("/login"))}><LogOut className="h-4 w-4" /></Button>
            </div>
          </div>
        </header>
        <main className="mx-auto max-w-6xl space-y-4 p-3 pb-24 md:p-6 md:pb-6">{children}</main>
      </div>
      <nav aria-label="Mobile" className="fixed inset-x-0 bottom-0 z-40 flex overflow-x-auto border-t bg-background md:hidden">
        {NAV.map(({ href, label, icon: Icon }) => (
          <Link key={href} href={href} aria-current={active(href) ? "page" : undefined}
            className={cn("flex min-w-[4.5rem] flex-1 flex-col items-center gap-0.5 px-2 py-2 text-[11px]", active(href) ? "font-semibold text-primary" : "text-muted-foreground")}>
            <Icon className="h-5 w-5" aria-hidden />{label}
          </Link>
        ))}
      </nav>
    </div>
  );
}
