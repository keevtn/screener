# premarket_panel

**Anchor:** `panel/premarket.py:149`

**Purpose:** Freezes one immutable premarket panel per session at 08:30 ET (I12): crank = materiality*exp(-age/6) + type weights + 0.25*high_alert; score adds finbert, buzz and earnings-today terms; top 25.

**Receives from:** [[score_clusters]] via [[cluster_scores]] — ranks off today's scored clusters.

**Receives from:** [[buzz_z]] via [[buzz_baselines]] — adds the social-buzz term.

**Receives from:** [[_persist_and_resolve]] via [[cluster_entities]] — ranks per attributed ticker.

**Feeds:** [[premarket_panel]] via [[premarket_panels]] — writes the frozen morning snapshot.

**Feeds:** [[grade_premarket_panels]] via [[premarket_panels]] — the snapshot graded after the close.

*Stage: 08 Panel*
