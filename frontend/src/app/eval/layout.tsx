import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "TAPE_ · Eval",
  robots: { index: false, follow: false },
};

export default function EvalLayout({ children }: { children: React.ReactNode }) {
  return children;
}
