# Results & methods appendix

Every headline number the submission deck cites, with its **definition**, exact
**data source** (table / query / script), where the **canonical record** lives,
and any **caveats**. All the data figures are regenerated live and read-only by
[`scripts/compute_deck_stats.py`](../scripts/compute_deck_stats.py):

```bash
# full recompute against the accumulated local DB (the slim seed omits some tables)
python scripts/compute_deck_stats.py --source ../Financial-News-Screener/data/pipeline.db
# against whatever DATABASE_URL points at (degrades per-stat when a table is absent)
python scripts/compute_deck_stats.py
```

Numbers **drift** as the ledger grows and load varies — the script prints the
CURRENT value honestly rather than reproducing the frozen deck figure. The table
below shows deck-cited vs a representative recompute (full local DB, 2026-07-31);
re-run the script for live values.

| Deck figure | Recompute (2026-07-31, full DB) | Source of truth |
|---|---|---|
| ingest→scored **38 s** | live median **77.5 s**, p90 526 s | script §latency |
| cohort **n=157 / 237 / 109 / 90 / 1,065** | 158 sim round-trips; 1,802 resolved real preds; 26,744 matured obs | script §cohorts + `grade/metrics.py`, `lab/analysis.py` |
| **47 %** hit rate | **48.4 %** | script §cohorts |
| **~ −1 %** / trade | **−0.83 %** net/trade (sim) | script §cohorts (`sim_trades.net_return`) |
| **~30 %** high-alert expansion | **+52.7 %** | script §cohorts |
| **462 → 0** a11y | 462 → 0 | [`docs/ada_compliance.md`](ada_compliance.md) |
| **~500** tests | 471 | script §repo |
| **67** endpoints | 67 | script §repo |
| **29** tables | 29 | script §repo |
| **~36k** LOC | 36,092 | script §repo |
| **~10k** items/day | 10,344 (last 24h) | script §throughput |

---

## Data-derived figures

### Ingest → scored latency ("38 seconds")
- **Definition:** seconds from a raw item's `raw_items.ingested_at` to its cluster's
  `cluster_scores.created_at` (the moment the score row is written). Median + p90.
- **Source/query:** `cluster_scores` ⋈ `clusters` (`origin_item_id`) ⋈ `raw_items`,
  `(julianday(cs.created_at) − julianday(ri.ingested_at)) × 86400`, over the most
  recent N=8,000 scored clusters. Live path filtered to `0 ≤ Δ < 3600 s` (backfilled /
  re-scored rows carry huge deltas and are excluded; the unfiltered median is printed
  too). Script `section_latency`.
- **Canonical record:** this script; also visible operationally as `/health`
  `staleness_seconds`.
- **Caveat:** latency **scales with scorer load and `SENTIMENT_MODE`** (onnx FinBERT
  on a throttled shared CPU is slower than the lexicon). 38 s was a healthy-window
  figure; current is ~77 s. Report the window, don't fudge.

### Per-gate cohorts ("n = 157 / 237 / 109 / 90 / 1,065")
- **Definition:** the pre-registered sample sizes of each evaluation gate at deck
  time — sim paper round-trips (~157–158), per-config graded prediction cohorts
  (237/109/90), and the matured-observation lab set (1,065). These are **frozen at
  pre-registration** and grow as more predictions grade / trades close.
- **Source:** sim round-trips = `sim_trades` (status `closed`); prediction cohorts =
  `predictions` grouped by `config_version` (status `graded`), via
  [`grade/metrics.py::metrics_by_config`](../src/pipeline/grade/metrics.py); the lab
  observation set = matured `signal_observations` via
  [`lab/analysis.py::load_lab_rows`](../src/pipeline/lab/analysis.py) (with the honest
  defaults: clean-window, holdout-excluded, per-ticker cap, per-event dedup, artifact
  screen). Script §cohorts prints current aggregates + a per-`config_version`
  breakdown so each gate's n is derivable.
- **Canonical record:** the gate parameters live in the `configs` table
  (`params_json`) and `configs/*.yaml`; the recompute path is the script + the two
  analysis modules above.
- **Caveat:** the deck n's are a snapshot; the live aggregate is larger.

### Hit rate ("47 %")
- **Definition:** `correct / (correct + incorrect)` over **resolved** graded
  predictions from **real** (non-baseline) configs. Expired (never crossed the
  threshold before horizon) are excluded from the denominator (that's `coverage`).
- **Source:** `predictions` where `status='graded'`, `outcome ∈ {correct,incorrect}`,
  `config_version` ∈ real set (a config is a baseline iff its `params_json` carries a
  `baseline` key). [`grade/metrics.py::compute_metrics`](../src/pipeline/grade/metrics.py).
- **Caveat:** measured vs the pre-registered baselines (always_up / random / momentum
  shadows); the point is real-vs-baseline separation, not the absolute level.

### Per-trade return ("~ −1 % / trade")
- **Definition:** mean **net** return per **closed sim paper round-trip** —
  `sim_trades.net_return`, which is gross move **minus the round-trip cost** (`COST_RT`).
- **Source:** `sim_trades` where `status='closed'`. Script §cohorts.
- **Caveat — do not conflate two different numbers:** the **ledger's**
  `predictions.realized_adjusted_return` is a *benchmark-adjusted, gross* grade of the
  signal (currently **+5.31 %/pred** over 2,443 real graded) and is NOT a P&L. The
  deck's "~ −1 %/trade" is the **sim paper P&L net of cost** (−0.83 %). The script
  prints both, labeled. Bracket-replay P&L used **forward-captured minute bars with an
  IEX backfill**; same-day `adj_close == close` (splits/divs re-adjust later — a
  documented approximation).

### High-alert expansion ("~30 %")
- **Definition:** how much larger the **next-day absolute move** (`|car_1d|`) is for
  **high-alert** clusters vs the **control** (non-high-alert), as a percentage
  expansion: `mean|car_1d|_highalert / mean|car_1d|_control − 1`.
- **Source:** matured `signal_observations`; `features_json.high_alert` splits the
  cohorts, `marks_json.car_1d` is the +1d cumulative abnormal return. Script §cohorts.
  `high_alert` itself is `materiality ≥ 0.70` (see [gate math] below).
- **Caveat:** cohort- and window-dependent; current is +52.7 % (high n=1,147 mean
  4.53 % vs control n=25,597 mean 2.96 %). Uses the clean/holdout-honest observation
  set, but expansion grows as more high-materiality events accumulate.

### Ingest volume ("~10k items/day")
- **Definition:** `raw_items` ingested per day. Script prints last-24h and the
  7-day mean.
- **Source:** `COUNT(*) FROM raw_items WHERE ingested_at >= now − 1 day`. Script
  §throughput.
- **Caveat:** volume swings with market hours / Bluesky firehose activity (last-24h
  10,344; 7-day mean ~8,800).

---

## Repo / engineering figures (recomputed from the tree)

| Figure | Definition & source | Canonical |
|---|---|---|
| **~500 tests** | `def test_` / `async def test_` under `tests/` (currently **471**). | `pytest` |
| **67 endpoints** | `@app.(get\|post\|put\|delete\|patch)(` decorators in `src/pipeline/api/app.py`. | that file |
| **29 tables** | `__tablename__ =` in `src/pipeline/common/models.py`. | that file |
| **~36k LOC** | line count of `src/` + `backend/` + `scripts/` `*.py` (24,366) plus `frontend/src` `*.ts/*.tsx` (11,726) = **36,092**. | script §repo |
| **10 frontend routes** | `page.tsx` files under `frontend/src/app`. | that tree |
| **462 → 0 a11y** | axe-core WCAG 2.1 AA critical+serious across all routes, before vs after remediation. Re-run `cd frontend && npm run a11y`. | [`docs/ada_compliance.md`](ada_compliance.md) |

## Gate math (definitions the cohorts depend on)
- **materiality** — flat per-catalyst-type constant from `configs/catalysts.yaml`
  (`default_materiality`: ma/fda 0.90/0.85 … 0.45); no tier/keyword boost.
  (`score/catalysts.py`.)
- **high_alert** — `materiality ≥ 0.70` (`high_alert_cutoff`, `configs/catalysts.yaml`).
- **sentiment / direction** — FinBERT `score = P(pos) − P(neg)`; LM `tanh(net/1.5)`;
  blended `0.6·finbert + 0.4·lm` (kind-adjusted); a prediction is directional when the
  attention-weighted composite `|s| ≥ 0.15` (`common/config.py`, `signal/engine.py`).
- **baseline shadows** — always_up / random / momentum configs (`grade/baselines.py`),
  identified by a `baseline` key in `configs.params_json`; the LEDGER hides them by
  default and the hit-rate cohort excludes them.

*Regenerate this appendix's numbers any time with `scripts/compute_deck_stats.py`.*
