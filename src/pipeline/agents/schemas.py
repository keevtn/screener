"""Strict output schemas for the agent layer (docs/ROADMAP.md tasks 7.2, 7.3).

The model is asked for JSON matching these shapes; the ranker validates with one
retry on failure (7.2). Keeping the schemas here (not inline) lets the tests assert
against the exact contract the model must satisfy.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Direction = Literal["bullish", "bearish", "neutral"]


class RankItem(BaseModel):
    """One ranked candidate — the exact schema from task 7.2."""

    ticker: str
    direction: Direction
    conviction: float = Field(ge=0.0, le=1.0)
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("ticker must be non-empty")
        return v


class RankerOutput(BaseModel):
    """The batched ranker response: a list of ranked candidates."""

    rankings: list[RankItem]


class DeepDiveEvidencePoint(BaseModel):
    """One cited piece of evidence in a deep dive — a point, optionally anchored to
    a cluster_id from the ticker's evidence bundle (unknown ids are nulled out)."""

    point: str
    cluster_id: str | None = None


class DeepDiveOutput(BaseModel):
    """Structured single-ticker deep-dive analysis (docs/ROADMAP.md task 7.4).

    A thesis + directional lean, cited key evidence, risks, and the disconfirming
    signals that would change the read. The model reads only our own data; it
    PROPOSES a view and cites cluster_ids so the analysis is auditable.
    """

    thesis: str
    direction: Direction
    conviction: float = Field(ge=0.0, le=1.0)
    key_evidence: list[DeepDiveEvidencePoint] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    what_would_change_my_mind: list[str] = Field(default_factory=list)


class AnalystOutput(BaseModel):
    """Weekly analyst response (task 7.3): a report plus an OPTIONAL config patch.

    proposed_patch is a flat {dotted.param.path: new_value} map applied to the base
    config params by scripts/approve.py — the analyst never writes config itself.
    An empty/None patch means "no change proposed this week".
    """

    report_md: str
    rationale: str
    proposed_patch: dict[str, object] = Field(default_factory=dict)
