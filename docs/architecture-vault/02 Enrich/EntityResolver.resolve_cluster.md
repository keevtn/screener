# EntityResolver.resolve_cluster

**Anchor:** `enrich/resolve.py:379`

**Purpose:** Resolves a cluster's origin text to tickers by ordered passes: cashtag (fuzzy>=92) -> exact name n-gram -> alias (longest phrase wins); blocklisted/common-word mentions become unmapped. match_method is the categorical confidence.

**Receives from:** [[persist_clusters]] via [[clusters]] — resolves from clusters.origin_item_id.

**Receives from:** [[SourceTiers.tier_of|tier_of]] — uses source tier only for origin tie-breaks upstream.

**Feeds:** [[_persist_and_resolve]] — returns matches + unmapped mentions.

*Stage: 02 Enrich*
