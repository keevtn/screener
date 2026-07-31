# sim_trades

*Table / hub node.*

**Holds:** Paper-trade ledger (entry/exit, broker order ids, cluster_id for provenance).

**Written by:** [[evaluate_entries]], [[AlpacaPaperBroker]], [[decide_exit]], [[EOD_flatten]]

**Read by:** [[pair_round_trips]], [[provenance_join]], [[PaperAccountReader]], [[reconcile_on_boot]], [[api_app]]

*Stage: 12 Tables*
