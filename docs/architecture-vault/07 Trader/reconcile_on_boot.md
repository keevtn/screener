# reconcile_on_boot

**Anchor:** `sim/driver.py:170`

**Purpose:** Read-only startup reconcile that logs orphan/missing positions between the broker and the ledger without mutating orders.

**Receives from:** [[run_trader_driver]] via [[sim_trades]] — compares ledger to broker at boot.

*Stage: 07 Trader*
