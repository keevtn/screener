# run_trader_driver

**Anchor:** `sim/driver.py`

**Purpose:** The trader's own process (TRADER_DRIVER_ENABLED): no HTTP route can touch an order; drives entries/exits on exchange-clock ET, boot-reconciles orphans read-only.

**Feeds:** [[evaluate_entries]] — the driver loop calls the entry pass.

**Feeds:** [[Heartbeat]] — emits liveness each loop.

**Feeds:** [[reconcile_on_boot]] — runs a read-only reconcile at startup.

*Stage: 07 Trader*
