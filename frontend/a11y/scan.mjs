/**
 * WCAG 2.1 AA accessibility scan for the TAPE_ terminal.
 *
 * Runs axe-core (via Playwright headless Chromium) over every route in
 * a11y/routes.json, aggregates violations by axe impact (critical > serious >
 * moderate > minor), prints a summary, and writes the full result to
 * a11y/report.json.
 *
 * Usage:
 *   npm run a11y                         # scans routes.json baseUrl (localhost:3000)
 *   A11Y_URL=https://example.app npm run a11y   # scans a deployed URL instead
 *
 * Exit code = number of (critical + serious) violations, so it can gate CI later
 * without any CI files here. Moderate/minor never fail the process.
 *
 * Note: automated axe covers ~30-50% of WCAG. Keyboard-only navigation and
 * screen-reader flows still need the manual passes documented in
 * docs/ada_compliance.md.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { AxeBuilder } from "@axe-core/playwright";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const cfg = JSON.parse(fs.readFileSync(path.join(HERE, "routes.json"), "utf8"));

const BASE = (process.env.A11Y_URL || cfg.baseUrl || "http://localhost:3000").replace(/\/$/, "");
const TAGS = cfg.tags || ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const SETTLE = cfg.settleMs ?? 2000;
const IMPACTS = ["critical", "serious", "moderate", "minor"];

function bucket() {
  return { critical: 0, serious: 0, moderate: 0, minor: 0, unknown: 0 };
}

async function main() {
  console.log(`\naxe-core WCAG 2.1 AA scan  ·  base: ${BASE}`);
  console.log(`tags: ${TAGS.join(", ")}\n`);

  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: cfg.viewport || { width: 1440, height: 900 } });

  const totals = bucket();
  const ruleTotals = new Map(); // ruleId -> { impact, help, wcag, count, routes:Set }
  const perRoute = [];

  for (const route of cfg.routes) {
    const url = BASE + route;
    const page = await context.newPage();
    // SSE + polling pages never reach networkidle, so settle on a fixed delay.
    try {
      await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
    } catch (e) {
      perRoute.push({ route, error: `goto: ${e.message}` });
      await page.close();
      console.log(`  ${route.padEnd(12)}  LOAD ERROR: ${e.message}`);
      continue;
    }
    await page.waitForTimeout(SETTLE);

    let result;
    try {
      result = await new AxeBuilder({ page }).withTags(TAGS).analyze();
    } catch (e) {
      perRoute.push({ route, error: `axe: ${e.message}` });
      await page.close();
      console.log(`  ${route.padEnd(12)}  AXE ERROR: ${e.message}`);
      continue;
    }

    const counts = bucket();
    for (const v of result.violations) {
      const impact = v.impact || "unknown";
      const nodes = v.nodes.length || 1;
      counts[impact] = (counts[impact] || 0) + nodes;
      totals[impact] = (totals[impact] || 0) + nodes;

      const wcag = (v.tags || [])
        .filter((t) => /^wcag\d/.test(t))
        .map((t) => t.replace(/^wcag/, "WCAG "))
        .join(", ");
      const cur = ruleTotals.get(v.id) || {
        impact,
        help: v.help,
        helpUrl: v.helpUrl,
        wcag,
        count: 0,
        routes: new Set(),
        samples: [],
      };
      cur.count += nodes;
      cur.routes.add(route);
      // Keep a handful of concrete nodes (target + failure data) to guide fixes.
      for (const n of v.nodes) {
        if (cur.samples.length >= 12) break;
        const data = (n.any && n.any[0] && n.any[0].data) || null;
        cur.samples.push({
          route,
          target: Array.isArray(n.target) ? n.target.join(" ") : String(n.target),
          summary: (n.failureSummary || "").replace(/\s+/g, " ").slice(0, 240),
          data,
        });
      }
      ruleTotals.set(v.id, cur);
    }
    perRoute.push({ route, counts, violations: result.violations });
    const line = IMPACTS.map((i) => `${i[0].toUpperCase()}:${counts[i]}`).join("  ");
    console.log(`  ${route.padEnd(12)}  ${line}`);
    await page.close();
  }

  await browser.close();

  // --- summary ---
  console.log(`\n${"=".repeat(60)}`);
  console.log("TOTAL violations (by axe impact, counted per element node):");
  for (const i of IMPACTS) console.log(`  ${i.padEnd(9)} ${totals[i]}`);
  console.log(`${"=".repeat(60)}`);

  const rules = [...ruleTotals.entries()]
    .map(([id, r]) => ({ id, ...r, routes: [...r.routes] }))
    .sort((a, b) => IMPACTS.indexOf(a.impact) - IMPACTS.indexOf(b.impact) || b.count - a.count);

  if (rules.length) {
    console.log("\nRules failed (worst impact first):");
    for (const r of rules) {
      console.log(
        `  [${(r.impact || "?").toUpperCase()}] ${r.id} ×${r.count}  (${r.routes.length} route(s))`,
      );
      console.log(`        ${r.help}  ${r.wcag ? "· " + r.wcag : ""}`);
    }
  } else {
    console.log("\nNo violations found. 🎉");
  }

  const report = {
    scannedAt: new Date().toISOString(),
    base: BASE,
    tags: TAGS,
    totals,
    rules,
    perRoute: perRoute.map((r) => ({ route: r.route, counts: r.counts, error: r.error })),
  };
  const outPath = path.join(HERE, "report.json");
  fs.writeFileSync(outPath, JSON.stringify(report, null, 2));
  console.log(`\nFull report → ${path.relative(process.cwd(), outPath)}`);

  const gating = (totals.critical || 0) + (totals.serious || 0);
  console.log(`\nGating (critical + serious): ${gating}`);
  process.exit(gating);
}

main().catch((e) => {
  console.error(e);
  process.exit(255);
});
