"""Ranker: candidates -> evidence -> model -> cited watchlist (docs/ROADMAP.md 7.2).

Proposes only (I6): writes to ranking_runs / rankings, never to configs or the
prediction ledger. Batches all candidate bundles into one cached-system call,
validates the strict JSON schema with exactly one retry, and logs spend per call.
A manual (force-run) invocation carries the operator's model + timeframe.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from pipeline.agents.candidates import (
    build_candidate_filter,
    default_ranker_candidates,
    select_candidates,
)
from pipeline.agents.client import (
    DEFAULT_RANKER_MODEL,
    LLMClient,
    enforce_daily_cap,
    is_opus,
    log_spend,
    resolve_model,
)
from pipeline.agents.evidence import build_evidence_bundle
from pipeline.agents.schemas import RankerOutput
from pipeline.common.models import Ranking, RankingRun
from pipeline.common.timeutil import utcnow

log = logging.getLogger("pipeline.agents.ranker")

RANKER_SYSTEM = """You are a disciplined equity news-ranking analyst.

You receive a JSON array of candidate tickers. Each candidate has a rolling news
"window" (decayed sentiment and materiality composites) and its recent scored news
"clusters" (finbert_score and lm_score in [-1, 1], catalyst_type, materiality in
[0, 1], and a cluster_id).

Rank ONLY the candidates that have a defensible directional read. For each, output:
- ticker: exactly as given
- direction: "bullish", "bearish", or "neutral"
- conviction: a float in [0, 1] (your confidence in the direction)
- rationale: 1-2 sentences grounded in the evidence
- evidence_ids: the cluster_id values you relied on (must come from that candidate's clusters)

Rules:
- Cite only cluster_ids present in the candidate you are ranking.
- Do not invent tickers or numbers. If a candidate is weak or mixed, either mark it
  "neutral" with low conviction or omit it.
- You PROPOSE a watchlist; you never place trades or change any configuration.

Return ONLY a JSON object of the form:
{"rankings": [{"ticker": "...", "direction": "...", "conviction": 0.0,
"rationale": "...", "evidence_ids": ["..."]}]}
No prose outside the JSON."""

_RETRY_SUFFIX = (
    "\n\nYour previous reply was not valid JSON for the required schema. "
    "Reply again with ONLY the JSON object, nothing else."
)


def _extract_json(text: str) -> str:
    """Pull the JSON object out of a model reply (tolerate ``` fences / prose)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text.strip()


def parse_ranker_output(text: str) -> RankerOutput:
    """Parse + validate; raises ValueError/ValidationError on bad output."""
    return RankerOutput.model_validate(json.loads(_extract_json(text)))


def _build_user_prompt(bundles: list[dict[str, Any]], horizon: int) -> str:
    return (
        f"Trading-day horizon: {horizon}.\n"
        f"Candidates (JSON):\n{json.dumps(bundles, ensure_ascii=False)}"
    )


def run_ranking(
    session: Session,
    client: LLMClient,
    *,
    params: dict[str, Any],
    config_version: str,
    filter_spec: dict[str, Any] | None = None,
    model: str = DEFAULT_RANKER_MODEL,
    horizon_trading_days: int | None = None,
    trigger: str = "manual",
    explicit_model: bool = False,
    limit: int | None = None,
    now: datetime | None = None,
    cap: float | None = None,
    max_tokens: int | None = None,
) -> RankingRun:
    """Run one ranking. Persists a RankingRun (+ Ranking rows + llm_spend).

    OPUS GUARD: automated runs must never burn Opus (cost). ``explicit_model`` is
    the operator's deliberate choice signal — True ONLY for a user-driven selection
    (the force-run dropdown, or an on-demand `--trigger manual --model opus` CLI
    run). It defaults False (fail-safe): any automated/scheduled/unmarked path that
    requests an Opus-class model is DOWNGRADED to the Sonnet default with a loud log
    line and a `model_requested`/`opus_downgraded` note in the run's filter_json —
    never silent. Explicit selections pass through untouched.

    `limit` (candidate breadth) defaults to AGENT_RANKER_CANDIDATES when unset.
    `max_tokens` scales with breadth when unset: the ranker emits one JSON object
    per ranked ticker (~160 output tokens each), so a wide run needs room or its
    output truncates into invalid JSON. Ceiling 16K covers the 150-candidate cap.
    A higher ceiling is free — you pay for tokens actually emitted, not the cap.
    """
    now = now or utcnow()
    model = resolve_model(model)
    model_requested = model
    opus_downgraded = False
    if not explicit_model and is_opus(model):
        log.warning(
            "OPUS GUARD — automated ranker run requested %s; automated runs never use "
            "Opus (cost). Downgrading to %s. Run Opus deliberately via the force-run "
            "dropdown or `run_ranker.py --trigger manual --model %s`.",
            model_requested, DEFAULT_RANKER_MODEL, model_requested,
        )
        model = resolve_model(DEFAULT_RANKER_MODEL)
        opus_downgraded = True
    horizon = int(horizon_trading_days or params["horizon_trading_days"])
    limit = limit if limit is not None else default_ranker_candidates()
    if max_tokens is None:
        max_tokens = min(16384, 2048 + limit * 160)
    filter_spec = dict(filter_spec) if filter_spec else build_candidate_filter()
    if opus_downgraded:
        # Provenance in the run record (filter_json already carries run metadata,
        # e.g. the premarket scope tag) — visible, never silent.
        filter_spec["model_requested"] = model_requested
        filter_spec["opus_downgraded"] = True

    enforce_daily_cap(session, cap=cap, now=now)  # refuse to start if over the soft cap

    tickers = select_candidates(session, filter_spec, limit=limit, now=now)
    run = RankingRun(
        created_at=now,
        trigger=trigger,
        model=model,
        horizon_trading_days=horizon,
        filter_json=filter_spec,
        candidate_count=len(tickers),
        config_version=config_version,
        status="ok",
    )
    session.add(run)
    session.flush()  # assign run.run_id for spend + ranking FKs

    if not tickers:
        run.status = "empty"
        session.commit()
        return run

    bundles = [build_evidence_bundle(session, t, params, config_version, now=now) for t in tickers]
    valid_ids = {c["cluster_id"] for b in bundles for c in b["clusters"]}
    valid_tickers = set(tickers)
    user = _build_user_prompt(bundles, horizon)

    parsed: RankerOutput | None = None
    for attempt in range(2):  # one retry on invalid JSON (7.2)
        prompt = user if attempt == 0 else user + _RETRY_SUFFIX
        try:
            result = client.complete(
                system=RANKER_SYSTEM, user=prompt, model=model, max_tokens=max_tokens
            )
        except Exception as exc:  # noqa: BLE001 — provider/transport failure (billing,
            # auth, rate limit, network). Persist the REASON as a failed run so the
            # RANK page shows it, instead of an unhandled 500 that loses its CORS
            # headers and surfaces in the browser as a bare "network error".
            run.status = "failed"
            run.error = f"llm call failed: {exc}"[:500]
            session.commit()
            return run
        try:
            parsed = parse_ranker_output(result.text)
            log_spend(session, result, purpose="rank", run_id=run.run_id, ok=True, now=now)
            break
        except (ValueError, ValidationError):
            log_spend(session, result, purpose="rank", run_id=run.run_id, ok=False, now=now)

    if parsed is None:
        run.status = "failed"
        run.error = "ranker output failed schema validation after one retry"
        session.commit()
        return run

    rank_no = 0
    for item in parsed.rankings:
        if item.ticker not in valid_tickers:
            continue  # drop hallucinated tickers
        rank_no += 1
        session.add(
            Ranking(
                run_id=run.run_id,
                rank=rank_no,
                ticker=item.ticker,
                direction=item.direction,
                conviction=item.conviction,
                rationale=item.rationale,
                evidence_ids_json=[e for e in item.evidence_ids if e in valid_ids],
            )
        )
    session.commit()
    return run
