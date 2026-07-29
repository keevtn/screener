"""Pydantic boundary schemas (docs/ROADMAP.md task 0.2).

I1 enforcement point: AwareDatetime rejects naive timestamps before anything
reaches the ORM.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from pipeline.common.models import RawItem, raw_item_id
from pipeline.common.timeutil import utcnow


class RawItemIn(BaseModel):
    """A raw item at the ingestion boundary; id is derived, never supplied."""

    model_config = ConfigDict(frozen=True)

    source: str = Field(min_length=1)
    source_class: Literal["structured", "social"]
    url: str | None = None
    guid: str | None = None
    published_at: AwareDatetime
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_identity(self) -> RawItemIn:
        if not (self.guid or self.url):
            raise ValueError("raw item needs a guid or url to derive its id")
        return self

    @property
    def id(self) -> str:
        return raw_item_id(self.source, guid=self.guid, url=self.url)

    def to_model(self) -> RawItem:
        return RawItem(
            id=self.id,
            source=self.source,
            source_class=self.source_class,
            url=self.url,
            published_at=self.published_at,
            ingested_at=utcnow(),
            payload_json=self.payload,
        )


class PredictionIn(BaseModel):
    """A prediction at issue time (the signal engine's output boundary, task 4.2)."""

    model_config = ConfigDict(frozen=True)

    ticker: str = Field(min_length=1, max_length=12)
    direction: Literal["bullish", "bearish"]
    confidence: float = Field(ge=0.0, le=1.0)
    horizon_trading_days: int = Field(gt=0)
    threshold: float = Field(gt=0.0)
    issued_at: AwareDatetime
    config_version: str
    evidence: dict[str, Any] = Field(default_factory=dict)
