# SourceTiers.tier_of

**Anchor:** `enrich/tiers.py:34`

**Purpose:** Maps a source string to an authority tier from source_tiers.yaml (default 2), used to pick a cluster's origin and to route sentiment weighting.

**Receives from:** entry point — external feed.

**Feeds:** [[EntityResolver.resolve_cluster|resolve_cluster]] — tier breaks origin ties.

**Feeds:** [[text_kind_of]] — tier decides filing vs press_release vs article weighting.

*Stage: 02 Enrich*
