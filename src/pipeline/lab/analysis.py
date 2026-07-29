"""Signal-lab analysis (docs/ROADMAP.md tasks 5c.4 + 5c.5).

Spearman IC (does the score predict forward abnormal return?), CAR curves by
sentiment bucket, and top-vs-bottom quintile spread — sliceable by catalyst type /
cap bucket / time. Defaults keep the analysis honest: clean-window only, backfilled
observations excluded (own slice on request), a per-ticker event cap so one ticker
can't dominate, and a frozen holdout that excludes the most recent N months (5c.5).
Bootstrap CIs via numpy (no new dependency).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.common.models import SignalObservation
from pipeline.common.timeutil import utcnow
from pipeline.lab.marking import CAR_HORIZONS

DEFAULT_HOLDOUT_MONTHS = 3
DEFAULT_PER_TICKER_CAP = 10
DEFAULT_SENTIMENT_BUCKET_THRESHOLD = 0.10
# Artifact guard: a CAR step between adjacent horizons larger than this (150%)
# is a split/adjustment break in the bar series, not a market move (observed:
# XAIR car_1d -7.8% -> car_3d +1,783% from a bar glitch). Flag, never fabricate.
SUSPECT_DELTA_CAR = 1.5


def marks_suspect(marks: dict[str, Any] | None) -> bool:
    """True when an observation's marks show a price-series artifact.

    Either the marking-time flag (``suspect_series``, set when the underlying
    bar series contains a single-day 4x jump — split/adjustment break) or the
    read-time symptom screen for rows marked before the flag existed: adjacent
    CAR horizons stepping by more than SUSPECT_DELTA_CAR.
    """
    m = marks or {}
    if m.get("suspect_series"):
        return True
    prev: float | None = None
    for k in CAR_HORIZONS:
        v = m.get(f"car_{k}d")
        if v is None:
            continue
        if prev is None and abs(v) > SUSPECT_DELTA_CAR:
            return True
        if prev is not None and abs(v - prev) > SUSPECT_DELTA_CAR:
            return True
        prev = v
    return False


def dedupe_per_event(observations: list[SignalObservation]) -> list[SignalObservation]:
    """One observation per (ticker, t0 calendar day): multiple clusters from the
    same underlying event otherwise pseudo-replicate it in every analysis
    (observed: XAIR x3 from one earnings event). Keeps the highest-materiality
    observation; ties break to the earliest t0."""
    best: dict[tuple[str, Any], SignalObservation] = {}
    for o in observations:
        key = (o.ticker, o.t0.date())
        cur = best.get(key)
        if cur is None:
            best[key] = o
            continue
        rank_new = ((o.features_json or {}).get("materiality") or 0.0, -o.t0.timestamp())
        rank_cur = ((cur.features_json or {}).get("materiality") or 0.0, -cur.t0.timestamp())
        if rank_new > rank_cur:
            best[key] = o
    return list(best.values())


def _rankdata(a: np.ndarray) -> np.ndarray:
    """Average-tie ranks (scipy.stats.rankdata algorithm, numpy-only)."""
    sorter = np.argsort(a, kind="mergesort")
    inv = np.empty(len(a), dtype=int)
    inv[sorter] = np.arange(len(a))
    ranked = a[sorter]
    obs = np.r_[True, ranked[1:] != ranked[:-1]]
    dense = obs.cumsum()[inv]
    counts = np.r_[np.nonzero(obs)[0], len(a)]
    return 0.5 * (counts[dense] + counts[dense - 1] + 1)


def _spearman(a: list[float], b: list[float]) -> float:
    """Spearman rho = Pearson correlation of the ranks (no scipy dependency)."""
    ra, rb = _rankdata(np.asarray(a, float)), _rankdata(np.asarray(b, float))
    if ra.std() == 0 or rb.std() == 0:
        return float("nan")
    return float(np.corrcoef(ra, rb)[0, 1])


def _feature(row: dict[str, Any], name: str) -> float | None:
    val = row.get(name)
    if val is None and name == "finbert_score":
        val = row.get("lm_score")
    return val


def _ret(row: dict[str, Any], horizon: int) -> float | None:
    return (row.get("marks") or {}).get(f"car_{horizon}d")


def load_lab_rows(
    session: Session,
    *,
    now: datetime | None = None,
    clean_only: bool = True,
    include_backfill: bool = False,
    include_holdout: bool = False,
    holdout_months: int = DEFAULT_HOLDOUT_MONTHS,
    catalyst_type: str | None = None,
    cap_bucket: str | None = None,
    per_ticker_cap: int | None = DEFAULT_PER_TICKER_CAP,
) -> list[dict[str, Any]]:
    """Matured observations as analysis rows, with the honest defaults applied
    (clean window, holdout, per-ticker cap, per-event dedup, artifact screen)."""
    now = now or utcnow()
    cutoff = now - timedelta(days=holdout_months * 30)
    matured = (
        session.execute(select(SignalObservation).where(SignalObservation.status == "matured"))
        .scalars()
        .all()
    )
    matured = dedupe_per_event(matured)
    rows: list[dict[str, Any]] = []
    for o in matured:
        if marks_suspect(o.marks_json):
            continue
        f = o.features_json or {}
        if not include_backfill and o.backfill:
            continue
        if clean_only and o.clean_window is False:
            continue
        if not include_holdout and o.t0 > cutoff:  # frozen holdout = recent months
            continue
        if catalyst_type and f.get("catalyst_type") != catalyst_type:
            continue
        if cap_bucket and f.get("cap_bucket") != cap_bucket:
            continue
        rows.append(
            {
                "ticker": o.ticker,
                "t0": o.t0,
                "finbert_score": f.get("finbert_score"),
                "lm_score": f.get("lm_score"),
                "catalyst_type": f.get("catalyst_type"),
                "cap_bucket": f.get("cap_bucket"),
                "backfill": o.backfill,
                "marks": o.marks_json or {},
            }
        )

    if per_ticker_cap:
        capped: list[dict[str, Any]] = []
        seen: dict[str, int] = defaultdict(int)
        for row in sorted(rows, key=lambda r: r["t0"]):
            if seen[row["ticker"]] < per_ticker_cap:
                capped.append(row)
                seen[row["ticker"]] += 1
        rows = capped
    return rows


def _pairs(rows: list[dict[str, Any]], horizon: int, feature: str) -> tuple[list, list]:
    feats, rets = [], []
    for row in rows:
        f, r = _feature(row, feature), _ret(row, horizon)
        if f is not None and r is not None:
            feats.append(float(f))
            rets.append(float(r))
    return feats, rets


def spearman_ic(
    rows: list[dict[str, Any]],
    horizon: int,
    feature: str = "finbert_score",
    *,
    bootstrap: int = 500,
    seed: int = 0,
) -> dict[str, Any]:
    """Spearman IC of feature vs forward CAR at a horizon, with a bootstrap CI."""
    feats, rets = _pairs(rows, horizon, feature)
    n = len(feats)
    if n < 3:
        return {"horizon": horizon, "feature": feature, "n": n, "ic": None}
    ic = _spearman(feats, rets)
    rng = np.random.default_rng(seed)
    boots = []
    for _ in range(bootstrap):
        idx = rng.integers(0, n, n)
        b = _spearman([feats[i] for i in idx], [rets[i] for i in idx])
        if b == b:  # skip nan (degenerate resample)
            boots.append(b)
    ci = np.percentile(boots, [2.5, 97.5]).tolist() if boots else [None, None]
    return {
        "horizon": horizon,
        "feature": feature,
        "n": n,
        "ic": None if ic != ic else round(float(ic), 4),
        "ci_low": None if ci[0] is None else round(float(ci[0]), 4),
        "ci_high": None if ci[1] is None else round(float(ci[1]), 4),
    }


def quintile_spread(
    rows: list[dict[str, Any]], horizon: int, feature: str = "finbert_score"
) -> dict[str, Any]:
    """Mean forward CAR of the top feature-quintile minus the bottom quintile."""
    feats, rets = _pairs(rows, horizon, feature)
    if len(feats) < 5:
        return {"horizon": horizon, "n": len(feats), "spread": None}
    order = np.argsort(feats)
    q = max(1, len(order) // 5)
    bottom = [rets[i] for i in order[:q]]
    top = [rets[i] for i in order[-q:]]
    return {
        "horizon": horizon,
        "feature": feature,
        "n": len(feats),
        "top_mean": round(float(np.mean(top)), 6),
        "bottom_mean": round(float(np.mean(bottom)), 6),
        "spread": round(float(np.mean(top) - np.mean(bottom)), 6),
    }


def car_curves(
    rows: list[dict[str, Any]],
    feature: str = "finbert_score",
    *,
    threshold: float = DEFAULT_SENTIMENT_BUCKET_THRESHOLD,
) -> dict[str, Any]:
    """Mean CAR at each horizon, grouped into bullish/neutral/bearish by feature."""
    buckets: dict[str, list[dict[str, Any]]] = {"bullish": [], "neutral": [], "bearish": []}
    for row in rows:
        f = _feature(row, feature)
        if f is None:
            continue
        key = "bullish" if f > threshold else "bearish" if f < -threshold else "neutral"
        buckets[key].append(row)
    out: dict[str, Any] = {"feature": feature, "horizons": list(CAR_HORIZONS), "curves": {}}
    for name, group in buckets.items():
        curve = []
        for h in CAR_HORIZONS:
            vals = [_ret(r, h) for r in group if _ret(r, h) is not None]
            curve.append(round(float(np.mean(vals)), 6) if vals else None)
        out["curves"][name] = {"n": len(group), "car": curve}
    return out
