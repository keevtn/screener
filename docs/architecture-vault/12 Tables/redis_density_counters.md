# redis_density_counters

*Table / hub node.*

**Holds:** Live per-ticker intraday item-hour counters (Redis INCR+TTL, fail-soft; the density input to heat).

**Written by:** [[RawItemHandler.write|write]]

**Read by:** [[screener_rows]]

*Stage: 12 Tables*
