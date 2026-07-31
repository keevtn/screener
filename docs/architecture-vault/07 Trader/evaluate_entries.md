# evaluate_entries

**Anchor:** `sim/engine.py:268`

**Purpose:** Paper-trade entries: for each enabled config x fresh scored cluster, _cluster_matches filters, _direction resolves (finbert_sign needs a non-null score; catalyst_typed uses the hint), a quote fills $1000/quote whole shares into sim_trades. One open per (config,ticker) + 24h cooldown; loss caps recomputed from the ledger each sweep.

**Receives from:** [[score_clusters]] via [[cluster_scores]] — trades off the same scores the signal reads, using fresh clusters.

**Receives from:** [[sim_configs]] via [[sim_configs]] — runs each enabled experiment config.

**Receives from:** [[AlpacaData]] — needs a live quote to fill.

**Receives from:** [[vol.atr_fraction|atr_fraction]] — snapshots entry ATR for volatility-based exits.

**Feeds:** [[AlpacaPaperBroker]] — submits the entry order.

**Feeds:** [[evaluate_entries]] via [[sim_trades]] — records the open paper position.

*Stage: 07 Trader*
