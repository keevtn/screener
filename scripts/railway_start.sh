#!/usr/bin/env bash
# Railway APP-service entrypoint (colocated worker + API over a shared SQLite volume).
#
# One process group, one container:
#   1. init_db          — create tables + append-only triggers (fast, BLOCKING: the
#                          API cannot serve without the schema).
#   2. hydrate_seed      — port the slim demo history (prediction ledger, graded
#                          outcomes, attention, universe, report cards) onto a FRESH
#                          volume, once (skips if the ledger is already non-empty).
#                          Bounded local copy, so it runs before the port bind.
#   3. (background)      — fetch_model (quantized FinBERT ONNX -> volume, only if
#                          SENTIMENT_MODE=onnx), then one-time seed_entities +
#                          snapshot_universe, then the live two-speed pipeline loop
#                          (ingest -> enrich -> score -> signal). Scoring backend is
#                          $SENTIMENT_MODE (lexicon default; onnx = int8 FinBERT).
#   4. serve_api (exec)  — the foreground web process; binds 0.0.0.0:$PORT, which is
#                          what Railway health-checks (GET /health).
#
# Seed/universe/model-fetch run behind the API so a slow SEC/Nasdaq/HF fetch never
# delays the port bind (a fresh deploy serves the seeded history immediately and an
# empty LIVE feed until the first sweep lands). All are idempotent, so re-running
# them on every restart is safe. The pipeline is a child of this script — if it dies
# the API stays up (degraded: no new news); Railway restarts the whole container
# only if the foreground API exits.
set -uo pipefail

# Run from the repo root so relative paths (scripts/, the data/ SQLite volume)
# resolve the same regardless of the CWD Railway invokes us with.
cd "$(dirname "$0")/.." || exit 1

# Make the `pipeline` package (src/ layout) importable WITHOUT a build/editable
# install: nixpacks installs deps from requirements.txt and does not `pip install .`,
# and several entry scripts (init_db, serve_api, seed_entities, snapshot_universe)
# do `from pipeline...` without touching sys.path themselves. Repo root is included
# too so backend/ flat modules resolve for any script that needs them.
export PYTHONPATH="$PWD/src:$PWD:${PYTHONPATH:-}"

export HOST=0.0.0.0
INTERVAL="${PIPELINE_INTERVAL:-300}"
# Scoring backend: lexicon (default, zero extra weight) | onnx (int8 FinBERT) |
# torch. onnx additionally needs FINBERT_ONNX_URL set so fetch_model can pull it.
export SENTIMENT_MODE="${SENTIMENT_MODE:-lexicon}"

echo "[boot] init_db (schema + triggers)"
python scripts/init_db.py

echo "[boot] hydrate_seed (port demo history if the ledger is empty)"
python scripts/hydrate_seed.py || echo "[boot] hydrate_seed failed; continuing (empty history)"

# Assign the vol_stop A/B exit policies to the trader configs (idempotent +
# walk-forward safe: only sets a config with no policy and no trades yet). Runs
# regardless of TRADER_DRIVER_ENABLED — it only edits config rows, never trades.
echo "[boot] assign_exit_policies (vol_stop A/B; idempotent)"
python scripts/assign_exit_policies.py || echo "[boot] assign_exit_policies failed; continuing (configs keep horizon_hold)"

(
  echo "[boot] fetch_model (quantized FinBERT ONNX; no-op unless SENTIMENT_MODE=onnx)"
  python scripts/fetch_model.py || echo "[boot] fetch_model failed; continuing (lexicon fallback)"
  echo "[boot] seed_entities (SEC CIK<->ticker upsert)"
  python scripts/seed_entities.py || echo "[boot] seed_entities failed; continuing"
  echo "[boot] snapshot_universe (Finviz -> Nasdaq fallback)"
  python scripts/snapshot_universe.py || echo "[boot] snapshot_universe failed; continuing"
  echo "[boot] pipeline loop (interval=${INTERVAL}s, SENTIMENT_MODE=${SENTIMENT_MODE})"
  python scripts/run_pipeline.py --interval "${INTERVAL}"
) &

# Standing paper-trading driver (optional, gated by TRADER_DRIVER_ENABLED, default
# OFF). Order placement lives ONLY in this process's internal clock loop — never in
# the API. It runs as its own child so a driver crash never touches the API/pipeline
# (Railway restarts the whole container only if the FOREGROUND api exits). On boot
# the driver reconciles Alpaca + the volume ledger so a mid-market redeploy never
# double-enters or exceeds caps (see pipeline/sim/driver.py). run_trader.py itself
# also no-ops unless the flag is truthy, so this is safe belt-and-suspenders.
case "${TRADER_DRIVER_ENABLED:-}" in
  1 | true | TRUE | True | yes | on)
    echo "[boot] TRADER driver ENABLED — launching standing daily loop"
    (python scripts/run_trader.py || echo "[boot] trader driver exited") &
    ;;
  *)
    echo "[boot] TRADER driver disabled (TRADER_DRIVER_ENABLED unset/false)"
    ;;
esac

echo "[boot] serve_api on 0.0.0.0:${PORT:-8001}"
exec python scripts/serve_api.py
