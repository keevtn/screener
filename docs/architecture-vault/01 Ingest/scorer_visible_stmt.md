# scorer_visible_stmt

**Anchor:** `ingest/shadow.py:18`

**Purpose:** The single I8 chokepoint: the only sanctioned SELECT over raw_items for scorers, hard-filtering source_class != 'social' so archived social content can never reach a prediction.

**Receives from:** [[RawItemHandler.write|write]] via [[raw_items]] — reads back only the structured rows.

**Feeds:** [[score_clusters]] — defines what the scoring path is allowed to see.

*Stage: 01 Ingest*
