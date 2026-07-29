"""Loaders for the static YAML/text config files under ``configs/``.

Distinct from ``config.py`` (the versioned, hashed *prediction* config landing in
task 4.3). These are the data-defined reference files: universe criteria, alias
overrides, watchlist. ROADMAP-NOTE: PyYAML is listed for Phase 2 in the roadmap
dependency table, but the config files it parses are introduced in Phase 0
(tasks 0.3/0.6), so pyyaml is pulled forward to the Phase 0 dependency set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# Repo-root/configs — this file is src/pipeline/common/config_files.py.
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIGS_DIR = _REPO_ROOT / "configs"


def configs_dir(override: str | Path | None = None) -> Path:
    return Path(override) if override else DEFAULT_CONFIGS_DIR


def load_aliases(path: str | Path | None = None) -> dict[str, str]:
    """``{alias -> TICKER}`` from configs/aliases.yaml (empty if absent)."""
    p = Path(path) if path else configs_dir() / "aliases.yaml"
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    overrides = data.get("overrides", {}) if isinstance(data, dict) else {}
    return {str(k): str(v).strip().upper() for k, v in overrides.items()}


def load_watchlist(path: str | Path | None = None) -> list[str]:
    """Upper-cased tickers from configs/watchlist.txt (blank/# lines dropped)."""
    p = Path(path) if path else configs_dir() / "watchlist.txt"
    if not p.exists():
        return []
    out: list[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line.upper())
    return out


def load_universe(path: str | Path | None = None) -> dict[str, Any]:
    """Parsed configs/universe.yaml (criteria, thresholds, always_include)."""
    p = Path(path) if path else configs_dir() / "universe.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
