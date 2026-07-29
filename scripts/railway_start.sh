#!/usr/bin/env bash
# Railway APP-service entrypoint (colocated worker + API over a shared SQLite volume).
#
# One process group, one container:
#   1. init_db          — create tables + append-only triggers (fast, BLOCKING: the
#                          API cannot serve without the schema).
#   2. (background)      — one-time seed_entities + snapshot_universe, then the live
#                          two-speed pipeline loop (ingest -> enrich -> score -> signal).
#                          LM-only scoring (--no-finbert) so it fits a small instance.
#   3. serve_api (exec)  — the foreground web process; binds 0.0.0.0:$PORT, which is
#                          what Railway health-checks (GET /health).
#
# Seed/universe run behind the API so a slow SEC/Nasdaq fetch never delays the port
# bind (a fresh deploy just serves an empty feed until the first sweep lands). Both
# are idempotent upserts, so re-running them on every restart is safe. The pipeline
# is a child of this script — if it dies the API stays up (degraded: no new news);
# Railway restarts the whole container only if the foreground API exits.
set -uo pipefail

export HOST=0.0.0.0
INTERVAL="${PIPELINE_INTERVAL:-300}"

echo "[boot] init_db (schema + triggers)"
python scripts/init_db.py

(
  echo "[boot] seed_entities (SEC CIK<->ticker upsert)"
  python scripts/seed_entities.py || echo "[boot] seed_entities failed; continuing"
  echo "[boot] snapshot_universe (Finviz -> Nasdaq fallback)"
  python scripts/snapshot_universe.py || echo "[boot] snapshot_universe failed; continuing"
  echo "[boot] pipeline loop (interval=${INTERVAL}s, LM-only)"
  python scripts/run_pipeline.py --interval "${INTERVAL}" --no-finbert
) &

echo "[boot] serve_api on 0.0.0.0:${PORT:-8001}"
exec python scripts/serve_api.py
