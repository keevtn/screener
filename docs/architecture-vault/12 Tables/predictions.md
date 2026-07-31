# predictions

*Table / hub node.*

**Holds:** The frozen signal ledger (open -> graded); grader fields writable only (I4).

**Written by:** [[SignalEngine.evaluate|evaluate]], [[resolve_armed_state]], [[emit_baselines]], [[apply_grade]]

**Read by:** [[grade_open_predictions]], [[Grader.grade|grade]], [[metrics_by_config]], [[emit_baselines]], [[backfill_prediction_context]], [[section_cohorts]]

*Stage: 12 Tables*
