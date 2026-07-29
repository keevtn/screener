import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "TAPE_ · Catalysts",
  robots: { index: false, follow: false },
};

// Theme + app-shell live in the root layout now; this only sets the page title.
export default function CatalystsLayout({ children }: { children: React.ReactNode }) {
  return children;
}
