# hydrate_seed

**Anchor:** `hydrate_seed.py`

**Purpose:** Per-table emptiness-gated, column-name-matched copy from the seed DB so a fresh volume comes up populated without clobbering live data.

**Receives from:** [[railway_start]] — invoked during bootstrap.

**Feeds:** [[hydrate_seed]] via [[sim_configs]] — seeds the 8 live experiment configs.

**Feeds:** [[hydrate_seed]] via [[configs]] — seeds the immutable signal config.

*Stage: 13 Ops*
