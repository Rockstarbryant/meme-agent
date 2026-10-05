"use client";
import type { ReactNode } from "react";
import { AuthProvider } from "@/lib/auth";
import { EventsProvider } from "@/lib/events";
import { ToastProvider } from "@/components/toast";

export function Providers({ children }: { children: ReactNode }) {
  return <AuthProvider><EventsProvider><ToastProvider>{children}</ToastProvider></EventsProvider></AuthProvider>;
}
