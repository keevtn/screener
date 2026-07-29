import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "TAPE_ · Universe",
  robots: { index: false, follow: false },
};

export default function UniverseLayout({ children }: { children: React.ReactNode }) {
  return children;
}
