# backfill_prediction_context

**Anchor:** `common/prediction_context.py`

**Purpose:** Carries each new prediction's origin source_class / headline / url into a companion table (spine step 16), incremental and idempotent so the LEDGER view can show why a prediction fired.

**Receives from:** [[SignalEngine.evaluate|evaluate]] via [[predictions]] — reads new predictions lacking a context row.

**Feeds:** [[backfill_prediction_context]] via [[prediction_context]] — writes the origin-news context for the LEDGER.

**Feeds:** [[api_app]] via [[prediction_context]] — the LEDGER view reads it.

*Stage: 05 Grade*
