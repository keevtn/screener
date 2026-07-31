# buzz_z

**Anchor:** `aggregate/attention.py:173`

**Purpose:** Standardized daily buzz = (social_count - mean) / max(std, 1); None without a baseline. Feeds the premarket panel and attention alerting.

**Receives from:** [[compute_buzz_baselines]] via [[buzz_baselines]] — reads the fitted baseline.

**Feeds:** [[premarket_panel]] — contributes the buzz term to the panel score.

*Stage: 03 Score*
