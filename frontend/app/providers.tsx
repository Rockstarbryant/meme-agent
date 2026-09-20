"use client";
import type { ReactNode } from "react";
import { AuthProvider } from "@/lib/auth";
import { EventsProvider } from "@/lib/events";

export function Providers({ children }: { children: ReactNode }) {
  return <AuthProvider><EventsProvider>{children}</EventsProvider></AuthProvider>;
}
