import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "TAPE_ · Ticker",
  robots: { index: false, follow: false },
};

export default function TickerLayout({ children }: { children: React.ReactNode }) {
  return children;
}
