# volume_guard

**Anchor:** `common/volume.py:213`

**Purpose:** Three-proof persistent-volume check (mount env+path, separate st_dev, data-beyond-seed); TRADER_VOLUME_GUARD=off kill-switch. The trader hard-refuses to place orders on an ephemeral container.

**Feeds:** [[run_trader_driver]] — must pass before the driver places any order.

**Feeds:** [[api_app]] — surfaces mount status on /health.

*Stage: 07 Trader*
