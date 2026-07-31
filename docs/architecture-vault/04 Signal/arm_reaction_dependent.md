# arm_reaction_dependent

**Anchor:** `signal/armed.py:66`

**Purpose:** Arms reaction-dependent catalysts (earnings, M&A) that can't be signed from text alone: writes an armed_states row per (ticker, cluster), structured-only (I8).

**Receives from:** [[score_clusters]] via [[cluster_scores]] — arms clusters flagged reaction_dependent because their direction needs the realized move.

**Feeds:** [[resolve_armed_state]] via [[armed_states]] — the armed rows awaiting a post-event reaction.

*Stage: 04 Signal*
