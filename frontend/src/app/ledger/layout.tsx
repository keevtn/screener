import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "TAPE_ · Ledger",
  robots: { index: false, follow: false },
};

export default function LedgerLayout({ children }: { children: React.ReactNode }) {
  return children;
}
