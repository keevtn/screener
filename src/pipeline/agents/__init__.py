"""Agent layer — ranker + weekly analyst (docs/ROADMAP.md Phase 7).

I6: nothing here is imported by the signal spine. LLMs rank and analyze only;
they PROPOSE (rankings, pending_changes) and never write configs or the ledger.
The one write path to config is scripts/approve.py (human-gated, I3).
"""

from pipeline.agents.analyst import apply_patch, run_analyst, validate_patch
from pipeline.agents.candidates import (
    build_candidate_filter,
    default_ranker_candidates,
    select_candidates,
)
from pipeline.agents.client import (
    ALLOWED_MODELS,
    DEFAULT_ANALYST_MODEL,
    DEFAULT_RANKER_MODEL,
    AnthropicClient,
    LLMClient,
    LLMResult,
    SoftCapExceeded,
    SubscriptionClient,
    compute_cost,
    default_client,
    default_daily_cap,
    enforce_daily_cap,
    log_spend,
    resolve_model,
    spend_since,
)
from pipeline.agents.deepdive import (
    DEFAULT_DEEP_DIVE_MODEL,
    DeepDiveRateLimited,
    assemble_evidence,
    deep_dive_rate_status,
    latest_analysis,
    parse_deep_dive_output,
    run_deep_dive,
)
from pipeline.agents.evidence import build_evidence_bundle
from pipeline.agents.ranker import parse_ranker_output, run_ranking
from pipeline.agents.schemas import AnalystOutput, DeepDiveOutput, RankerOutput, RankItem

__all__ = [
    "ALLOWED_MODELS",
    "DEFAULT_ANALYST_MODEL",
    "DEFAULT_DEEP_DIVE_MODEL",
    "DEFAULT_RANKER_MODEL",
    "AnalystOutput",
    "AnthropicClient",
    "DeepDiveOutput",
    "DeepDiveRateLimited",
    "LLMClient",
    "LLMResult",
    "RankItem",
    "RankerOutput",
    "SoftCapExceeded",
    "SubscriptionClient",
    "apply_patch",
    "assemble_evidence",
    "build_candidate_filter",
    "build_evidence_bundle",
    "compute_cost",
    "deep_dive_rate_status",
    "default_client",
    "default_daily_cap",
    "default_ranker_candidates",
    "enforce_daily_cap",
    "latest_analysis",
    "log_spend",
    "parse_deep_dive_output",
    "parse_ranker_output",
    "resolve_model",
    "run_analyst",
    "run_deep_dive",
    "run_ranking",
    "select_candidates",
    "spend_since",
    "validate_patch",
]
