import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        "fin-bg":     "#0a0e1a",
        "fin-card":   "#0f1629",
        "fin-border": "#1e2d4a",
        "fin-accent": "#00d4aa",
        // TAPE_ terminal palette (Screener Suite). Scoped by usage under the
        // `.tape` root; kept separate from the fin-* news-feed theme.
        tape: {
          bg:        "#07090D",
          panel:     "#0B0D12",
          "panel-2": "#0D1017",
          rail:      "#0A0D13",
          border:    "#1C2230",
          "border-soft": "#151A24",
          line:      "#10141C",
          text:      "#E6EAF2",
          sub:       "#B9C0CF",
          muted:     "#8B94A7",
          faint:     "#5A6478",
          dim:       "#3E4656",
          accent:    "#4FD1C5",
          "accent-hi": "#8AE8DF",
          bull:      "#34D399",
          bear:      "#FB7185",
          warn:      "#F0B44A",
        },
      },
    },
  },
  plugins: [],
};
export default config;
