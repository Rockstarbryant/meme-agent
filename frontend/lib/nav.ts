import { Activity, Bot, ScrollText, Briefcase, LayoutDashboard, Radar, Search, Settings, SlidersHorizontal, Wallet, type LucideIcon } from "lucide-react";

export interface NavItem { href: string; label: string; icon: LucideIcon }
export interface NavGroup { label: string; items: NavItem[] }

/** Desktop sidebar: grouped by what the user is doing. */
export const NAV_GROUPS: NavGroup[] = [
  { label: "Trade", items: [
    { href: "/", label: "Dashboard", icon: LayoutDashboard },
    { href: "/opportunities", label: "Opportunities", icon: Radar },
    { href: "/discovery", label: "Discovery", icon: Search },
    { href: "/positions", label: "Positions", icon: Briefcase },
  ] },
  { label: "Agent", items: [
    { href: "/agent", label: "Agent", icon: Bot },
    { href: "/strategies", label: "Strategies", icon: SlidersHorizontal },
    { href: "/activity", label: "Activity", icon: Activity },
  ] },
  { label: "Account", items: [
    { href: "/wallet", label: "Wallet", icon: Wallet },
    { href: "/settings", label: "Settings", icon: Settings },
  ] },
];

/** Shown only to admins (users.is_admin or ADMIN_EMAILS on the server). The server also enforces it (404 for everyone else). */
export const ADMIN_GROUP: NavGroup = { label: "Admin", items: [{ href: "/admin/audit", label: "Audit log", icon: ScrollText }] };

/** Mobile bottom bar: the four screens used all day, plus "More" for the rest (nine tabs did not fit a phone). */
export const MOBILE_PRIMARY: NavItem[] = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/opportunities", label: "Opportunities", icon: Radar },
  { href: "/positions", label: "Positions", icon: Briefcase },
  { href: "/agent", label: "Agent", icon: Bot },
];
export const MOBILE_MORE: NavItem[] = NAV_GROUPS.flatMap((g) => g.items).filter((i) => !MOBILE_PRIMARY.some((p) => p.href === i.href));

export function isActive(path: string, href: string): boolean {
  return href === "/" ? path === "/" : path === href || path.startsWith(href + "/");
}
