import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "TAPE_ · Trader",
  robots: { index: false, follow: false },
};

export default function TraderLayout({ children }: { children: React.ReactNode }) {
  return children;
}
