# ADA / WCAG 2.1 AA Compliance — TAPE_ Terminal

Accessibility audit and remediation of the deployed screener terminal.

- **Frontend:** https://hospitable-youth-production-7d20.up.railway.app
- **API:** https://screener-production-1ecc.up.railway.app
- **Standard targeted:** WCAG 2.1 Level AA (axe tags `wcag2a`, `wcag2aa`, `wcag21a`, `wcag21aa`)
- **Tooling:** [axe-core](https://github.com/dequelabs/axe-core) via Playwright (headless Chromium)
- **Routes scanned:** `/`, `/screener`, `/universe`, `/catalysts`, `/rank`, `/ledger`, `/eval`, `/config`
- **Date:** 2026-07-29

Re-run any time with the committed harness (see [Running the scan](#running-the-scan)).

---

## Result summary

| Severity (axe impact) | Before | After |
|-----------------------|-------:|------:|
| Critical              |      2 |     0 |
| Serious               |    460 |     0 |
| Moderate              |      0 |     0 |
| Minor                 |      0 |     0 |
| **Total**             | **462**| **0** |

Counts are per **element node** (one rule can fail on many elements). Automated
axe violations went to **zero** across all eight routes after remediation.

**Before** was scanned against the live deployment; **after** against a local
production build (`next build && next start`) carrying the fixes, with the
`NEXT_PUBLIC_*` API URLs pointed at the live backend so pages render real data.
The fixes all live in shared design tokens / shared components, so the comparison
holds across the whole terminal. The after-build was validated on data-heavy
routes to confirm it was not a false pass on empty pages (e.g. `/universe`
rendered 51 rows and 324 de-emphasized-text nodes — all passing).

### Before — violations per route (live)

| Route        | Critical | Serious |
|--------------|---------:|--------:|
| `/`          |        0 |      59 |
| `/screener`  |        2 |       1 |
| `/universe`  |        0 |     127 |
| `/catalysts` |        0 |      97 |
| `/rank`      |        0 |      40 |
| `/ledger`    |        0 |      86 |
| `/eval`      |        0 |      28 |
| `/config`    |        0 |      22 |

After remediation every route reports **0 / 0 / 0 / 0**.

---

## Findings by rule (before) and disposition

### 1. `color-contrast` — Serious ×458 — WCAG 1.4.3 Contrast (Minimum), AA
**7 routes.** Small de-emphasized terminal text failed the 4.5:1 minimum for
normal text on the darkest panel surface (`#0D1017`). Two palette tokens were the
culprits — everything else in the palette already passed:

| Token         | Used for                    | Old value  | Ratio (old) | New value  | Ratio (new) |
|---------------|-----------------------------|-----------:|------------:|-----------:|------------:|
| `tape.faint`  | de-emphasized labels/values | `#5A6478`  |     3.2 : 1 | `#7E8A9E`  |     5.5 : 1 |
| `tape.dim`    | faintest hints / meta text  | `#3E4656`  |     2.0 : 1 | `#757F91`  |     4.7 : 1 |

**Fixed.** Raised the two tokens in `frontend/tailwind.config.ts`. This was a
minimal token change (not a restyle) — one edit clears all 458 node failures. The
de-emphasis hierarchy `muted (6.2:1) > faint (5.5:1) > dim (4.7:1)` is preserved,
and the cool blue-grey hue is retained so the TAPE aesthetic is unchanged. Tokens
that already passed (`text` 15.8, `sub` 10.4, `muted` 6.2, `accent` 10.2, `bull`
9.9, `bear` 7.1, `warn` 10.3) were left untouched. `tape.border` (1.2:1) is used
only for non-text rules/dividers, which are exempt from 1.4.3.

### 2. `select-name` — Critical ×2 — WCAG 4.1.2 Name, Role, Value, A
**`/screener`.** The SECTOR and INDUSTRY `<select>` dropdowns in the screener
filter bar had a visible caption that was not *programmatically* associated with
the control, so assistive tech announced them as unnamed.

**Fixed.** Added `aria-label={label}` to the shared `Sel` `<select>` in
`frontend/src/components/tape/ScreenerFilterBar.tsx` (covers both dropdowns).

### 3. `scrollable-region-focusable` — Serious ×2 — WCAG 2.1.1 Keyboard, A
Two scroll containers had no keyboard access (a mouse-only horizontal/vertical
scroll):
- `/screener` — the horizontal column scroller (`.tape-xscroll` in
  `ScreenerTable.tsx`).
- `/config` — the detail `<section>`.

**Fixed.** Made both focusable and named: `tabIndex={0}` + `role="region"` +
`aria-label` on the screener scroller; `tabIndex={0}` + `aria-label` on the config
section. Keyboard users can now Tab to the region and scroll with arrow keys.

### Also improved (proactive, during the ledger rework)
Not flagged, but hardened while editing the LEDGER page:
- `aria-pressed` on the status filter buttons + `role="group"` / `aria-label` on
  the control group (button state now exposed to AT).
- `scope="col"` on the ledger table header cells (explicit header association).

---

## Deferred / not fixed

**Nothing at Critical or Serious was deferred** — all automated Critical + Serious
findings are fixed and re-verified at zero.

- **Moderate / Minor:** none were reported under the WCAG 2.1 AA tag set.
- **No redesign was required.** All fixes were token nudges or attribute additions;
  the terminal's visual design is unchanged.

Items intentionally **out of scope for this automated pass** (they need the manual
review below, not code changes we could make blindly):
- Full keyboard traversal order and focus management inside the ticker modal /
  overlays.
- Screen-reader announcement of live-updating regions (the SSE-driven tape and
  live quotes are not yet `aria-live` regions — candidate follow-up, but it risks
  noisy announcements and needs UX judgement, so it is deferred to a manual pass).

---

## Automated coverage caveat + manual checklist

Automated tooling (axe-core) reliably catches roughly **30–50%** of WCAG success
criteria — contrast, names/roles, landmark/heading structure, ARIA misuse. It
**cannot** verify the experiential criteria. A zero-violation automated scan is
necessary but **not sufficient** for AA conformance. The following require manual
passes:

**Keyboard-only (no mouse):**
- [ ] Tab reaches every interactive control (nav, tabs, filters, selects, table
      links, pagination) in a sensible order; nothing is a keyboard trap.
- [ ] The skip-link appears on first Tab and jumps to main content.
- [ ] The horizontal screener scroller and config section scroll via arrow keys
      once focused (the fix above enables this — confirm by hand).
- [ ] The ticker modal: focus moves into it on open, is trapped while open, and
      returns to the trigger on close (Esc closes it).
- [ ] Visible focus indicator on every focusable element (the `:focus-visible`
      2px outline is defined — confirm it is never clipped by `overflow:hidden`).

**Screen reader (NVDA / VoiceOver):**
- [ ] Page `<title>` and a single top-level heading per route are announced.
- [ ] Tables are announced with their column headers (row/col context).
- [ ] Status/filter buttons announce pressed/unpressed state.
- [ ] Live tape / quote updates are perceivable without being overwhelming
      (decide on `aria-live="polite"` vs. an on-demand "what changed" affordance).
- [ ] External article links in the LEDGER announce that they open in a new tab.

**Visual / cognitive:**
- [ ] 200% browser zoom and 400% reflow: no loss of content or horizontal scroll
      of the page body (the app uses relative units + `overflow-x:hidden` on body).
- [ ] Meaning is never conveyed by color alone — direction uses ▲/▼ glyphs + text,
      outcomes use words; confirm no color-only cell remains.
- [ ] `prefers-reduced-motion` respected (the pulse/glow animations are already
      gated — confirm on the live tape).
- [ ] Content is legible in both the default dark theme and any forced-colors /
      high-contrast OS mode.

---

## Running the scan

The harness is committed under `frontend/a11y/` and wired to an npm script.

```bash
cd frontend
npm install            # first time: pulls @axe-core/playwright + playwright
npx playwright install chromium   # first time: the headless browser

# scan the local dev/production server (default baseUrl in a11y/routes.json)
npm run a11y

# scan a deployed URL instead
A11Y_URL=https://hospitable-youth-production-7d20.up.railway.app npm run a11y
```

- **Config:** `frontend/a11y/routes.json` — base URL, WCAG tag set, viewport, and
  the route list. Edit here to add routes or change the standard.
- **Runner:** `frontend/a11y/scan.mjs` — runs axe over each route, prints a
  per-route + per-rule summary, and writes the full result to `a11y/report.json`
  (git-ignored).
- **CI-ready:** the process exits with a code equal to the number of
  `critical + serious` violations, so it can gate a pipeline later without any CI
  files added here. Moderate/minor never fail the process.

**Note on dynamic pages:** the terminal uses SSE + polling, so pages never reach
`networkidle`; the runner settles on a fixed delay (`settleMs` in `routes.json`)
after `domcontentloaded`. If a route's data is slow to load in headless, bump
`settleMs`.
