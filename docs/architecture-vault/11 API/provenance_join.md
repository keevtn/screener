# provenance_join

**Anchor:** `api/trader.py:273`

**Purpose:** Traces an order back to its cause: order id -> sim_trades.broker_*_order_id -> cluster_id -> clusters.origin_item_id -> raw_items.title.

**Receives from:** [[pair_round_trips]] via [[sim_trades]] — starts from the matched trade.

**Receives from:** [[persist_clusters]] via [[clusters]] — hops through the cluster.

**Receives from:** [[RawItemHandler.write|write]] via [[raw_items]] — ends at the origin headline.

**Feeds:** [[api_app]] — served on /trader/blotter with full provenance.

*Stage: 11 API*
