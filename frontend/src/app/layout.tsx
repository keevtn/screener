/* eslint-disable @next/next/no-page-custom-font -- runtime <link> keeps offline
   builds working and lets globals.css reference the font by its real family name
   ("IBM Plex Mono"); it degrades to system mono/sans if unreachable. */
import type { Metadata, Viewport } from "next";
import "./globals.css";
import { TickerModalProvider } from "@/components/tape/TickerModalProvider";
import ChunkReloadGuard from "@/components/tape/ChunkReloadGuard";

export const metadata: Metadata = {
  title: "TAPE_ · Terminal",
  description: "Financial news + prediction terminal",
  icons: {
    icon: "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg'/>",
  },
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: "#07090d",
};

/**
 * Root layout — the whole app is the TAPE_ terminal now. Fonts + the `.tape`
 * theme scope + the app-shell flex column live here, so every route (LIVE feed,
 * Screener, Catalysts, AI Rank) renders inside one terminal surface with TapeNav.
 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link
          rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap"
        />
        <ChunkReloadGuard />
        <TickerModalProvider>
          <div className="tape app-shell flex flex-col overflow-hidden">{children}</div>
        </TickerModalProvider>
      </body>
    </html>
  );
}
