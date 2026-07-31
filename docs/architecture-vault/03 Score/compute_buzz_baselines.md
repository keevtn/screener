# compute_buzz_baselines

**Anchor:** `aggregate/attention.py:113`

**Purpose:** Fits per-ticker social-volume baselines: winsorize at the 95th pct, empirical-Bayes shrink toward the global prior (k=10), min 3 days, STD_FLOOR 1.0.

**Receives from:** [[build_attention_daily]] via [[attention_daily]] — reads social_count history because a baseline needs the trailing series.

**Feeds:** [[buzz_z]] via [[buzz_baselines]] — the mean/std buzz_z standardizes against.

*Stage: 03 Score*
