"""Source provenance tiers (docs/ROADMAP.md task 2.3).

Loads configs/source_tiers.yaml and maps a source label to a tier (lower = more
authoritative). Cluster origin selection uses this to break published_at ties, and
aggregation later honors ``tier3_handling`` (down_weight vs drop).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from pipeline.common.config_files import configs_dir


class SourceTiers:
    def __init__(
        self,
        tiers: dict[int, list[str]],
        *,
        default_tier: int = 2,
        tier3_handling: str = "down_weight",
    ) -> None:
        # Precompute (tier, lowercased-pattern) in ascending tier order so the
        # most authoritative match wins when a label matches multiple patterns.
        self._patterns: list[tuple[int, str]] = []
        for tier in sorted(tiers):
            for pat in tiers[tier]:
                self._patterns.append((tier, pat.lower()))
        self.default_tier = default_tier
        self.tier3_handling = tier3_handling

    def tier_of(self, source: str) -> int:
        s = (source or "").lower()
        for tier, pat in self._patterns:  # ascending tier -> first match is lowest
            if pat in s:
                return tier
        return self.default_tier


def load_source_tiers(path: str | Path | None = None) -> SourceTiers:
    p = Path(path) if path else configs_dir() / "source_tiers.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    raw_tiers = data.get("tiers", {}) or {}
    tiers = {int(k): list(v) for k, v in raw_tiers.items()}
    return SourceTiers(
        tiers,
        default_tier=int(data.get("default_tier", 2)),
        tier3_handling=str(data.get("tier3_handling", "down_weight")),
    )
