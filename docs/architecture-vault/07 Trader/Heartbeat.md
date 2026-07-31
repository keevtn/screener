# Heartbeat

**Anchor:** `sim/driver.py:98`

**Purpose:** Writes the single trader_heartbeat row (driver_id host:pid:epoch) so a second driver is detected and surfaced on /trader/driver.

**Receives from:** [[run_trader_driver]] — pulsed each driver loop.

**Feeds:** [[Heartbeat]] via [[trader_heartbeat]] — writes the liveness row.

**Feeds:** [[api_app]] via [[trader_heartbeat]] — /trader/driver reads it for double-driver detection.

*Stage: 07 Trader*
