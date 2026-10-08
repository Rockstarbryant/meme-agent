import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { IBM_Plex_Mono, Playfair_Display, Source_Sans_3 } from "next/font/google";
import { Providers } from "./providers";
import "./globals.css";

// Editorial type system: Playfair Display for headlines and figures, Source Sans 3 for body and UI, IBM Plex Mono for labels.
const serif = Playfair_Display({ subsets: ["latin"], variable: "--font-serif", display: "swap" });
const sans = Source_Sans_3({ subsets: ["latin"], variable: "--font-sans", display: "swap" });
const mono = IBM_Plex_Mono({ subsets: ["latin"], weight: ["400", "500"], variable: "--font-mono", display: "swap" });

export const metadata: Metadata = { title: "Arc Agent", description: "Non-custodial, policy-controlled AI trading agent for Arc. PAPER and LIVE are always labelled." };
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  colorScheme: "light dark",
  themeColor: [{ media: "(prefers-color-scheme: light)", color: "#FAFAF8" }, { media: "(prefers-color-scheme: dark)", color: "#141311" }],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return <html lang="en" className={`${serif.variable} ${sans.variable} ${mono.variable}`}><body><Providers>{children}</Providers></body></html>;
}
