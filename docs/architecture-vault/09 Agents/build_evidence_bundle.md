# build_evidence_bundle

**Anchor:** `agents/evidence.py:28`

**Purpose:** Builds the evidence bundle whose cluster_ids are the ONLY valid citations; any ranking that cites outside the bundle is dropped as a hallucination.

**Receives from:** [[select_candidates]] — bundles the selected candidates.

**Receives from:** [[WindowAccumulator]] — reuses the same decayed evidence the signal sees.

**Feeds:** [[run_ranking]] — the grounded evidence the ranker must cite.

*Stage: 09 Agents*
