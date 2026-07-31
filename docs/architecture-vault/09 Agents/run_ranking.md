# run_ranking

**Anchor:** `agents/ranker.py:94`

**Purpose:** One batched LLM call that ranks the candidate clusters, writing ranking_runs + rankings; citations are validated against the evidence bundle.

**Receives from:** [[build_evidence_bundle]] — ranks only within the grounded bundle.

**Receives from:** [[default_client]] via [[llm_spend]] — spends against the daily budget.

**Feeds:** [[run_ranking]] via [[ranking_runs]] — writes the run header.

**Feeds:** [[run_ranking]] via [[rankings]] — writes the ranked rows.

**Feeds:** [[api_app]] via [[rankings]] — served on /agents/rankings.

*Stage: 09 Agents*
