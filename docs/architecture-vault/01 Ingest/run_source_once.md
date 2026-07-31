# run_source_once

**Anchor:** `scripts/dispatch.py:122`

**Purpose:** Per-source dispatch used identically by the manual CLI and the pipeline loop: opens a backend _HttpClient, drives that source's _poll_* helpers, and pipes every parsed item into the sink.

**Receives from:** [[run_cycle]] — called once per source each ingest step.

**Feeds:** [[RawItemHandler.write|write]] — every parsed NewsItem is handed to the sink.

*Stage: 01 Ingest*
