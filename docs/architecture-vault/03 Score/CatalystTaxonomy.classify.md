# CatalystTaxonomy.classify

**Anchor:** `score/catalysts.py:62`

**Purpose:** Config-driven catalyst classifier (I11): pass 1 matches SEC filing-type / EDGAR item codes, pass 2 headline keywords. Sets type, materiality, direction hint and high_alert = materiality>=0.70. Taxonomy runs ma .90 down to lockup_expiry .40.

**Receives from:** [[score_clusters]] — classifies each cluster's origin item.

**Feeds:** [[score_clusters]] — returns catalyst_type/materiality/direction/high_alert onto the score row.

*Stage: 03 Score*
