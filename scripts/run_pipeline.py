"""Full-pipeline orchestrator (docs/ROADMAP.md -- the hands-off measurement-window loop).

Two-speed loop. FAST deterministic sweeps (ingest -> enrich -> score/catalysts ->
observe -> signal) run every --fast-interval so new items reach scored clusters,
heat inputs, and signals within ~2 minutes; a FULL sweep (adds baselines, grading,
lab marking, attention/buzz rollup) runs every --interval. start.ps1 launches this
loop by default (its own window). Every step is idempotent, so a crashed or
repeated sweep is safe. Grading stays honest (I12/I3): predictions mature on their
real horizon -- the fast cadence is scoring/signal freshness, never early outcomes.

    python scripts/run_pipeline.py --once                          # one full cycle
    python scripts/run_pipeline.py --interval 300                  # 2-min fast / 5-min full
    python scripts/run_pipeline.py --interval 300 --no-finbert     # LM-only (low RAM)

WHERE: run on a machine that does not sleep (PC with sleep disabled, or a VPS).
FinBERT needs ~440MB RAM; pass --no-finbert to score LM-only on constrained hosts.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
try:
    from dotenv import load_dotenv

    load_dotenv(_REPO / ".env")
except ImportError:
    pass
for _p in (_REPO / "scripts", _REPO / "backend"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from dispatch import run_source_once  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from pipeline.aggregate.attention import (  # noqa: E402
    build_attention_daily,
    compute_buzz_baselines,
)
from pipeline.common.config import get_or_create_config  # noqa: E402
from pipeline.common.db import ensure_indexes, make_engine  # noqa: E402
from pipeline.common.events import publish_event  # noqa: E402
from pipeline.common.models import Entity, Prediction, SignalObservation  # noqa: E402
from pipeline.common.prediction_context import backfill_prediction_context  # noqa: E402
from pipeline.enrich.backfill import backfill_enrichment  # noqa: E402
from pipeline.enrich.resolve import EntityResolver  # noqa: E402
from pipeline.enrich.tiers import load_source_tiers  # noqa: E402
from pipeline.grade.baselines import emit_baselines  # noqa: E402
from pipeline.grade.job import grade_open_predictions  # noqa: E402
from pipeline.ingest import RawItemHandler  # noqa: E402
from pipeline.lab.marking import mark_observations  # noqa: E402
from pipeline.lab.observe import observe_scored_clusters  # noqa: E402
from pipeline.marketdata import MarketDataProvider  # noqa: E402
from pipeline.panel import roll_event_status  # noqa: E402
from pipeline.score.score import score_clusters  # noqa: E402
from pipeline.score.sentiment import default_lm, resolve_finbert  # noqa: E402
from pipeline.signal.cycle import run_signal_cycle  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("run_pipeline")

STRUCTURED_SOURCES = ("rss", "sec", "fda")
# Social archival lanes (shadow-mode I8: raw_items only, never scored). Polled
# on FULL sweeps only. Bluesky term-search (the firehose fallback) + Reddit OAuth;
# both are env/degradation fail-soft (Reddit log-and-skips without creds).
SOCIAL_ARCHIVE_SOURCES = ("bluesky", "reddit")


def _step(name: str, fn, *, session=None) -> None:
    """Run one pipeline step, logging its result and never crashing the cycle.

    On failure the shared session is ROLLED BACK — without this, one failed step
    (e.g. a lost SQLite lock race) leaves the session in PendingRollback and
    every later step in the cycle fails with it (observed: score lock -> signal/
    baselines/grade/mark all dead for the sweep).
    """
    start = time.monotonic()
    try:
        result = fn()
        log.info("step %-10s ok  (%.1fs) %s", name, time.monotonic() - start, result or "")
    except Exception as exc:  # noqa: BLE001 -- a bad step must not kill the loop
        log.warning("step %-10s FAILED (%.1fs): %s", name, time.monotonic() - start, exc)
        if session is not None:
            with contextlib.suppress(Exception):
                session.rollback()


def run_cycle(
    engine,
    *,
    resolver,
    tiers,
    provider,
    lm,
    finbert,
    sources,
    market: bool,
    attention_engines=None,
    heavy: bool = True,
    rescore_all: bool = False,
) -> None:
    """One sweep. heavy=False is the FAST deterministic sweep — ingest, enrich,
    score (catalysts + sentiment -> heat inputs), observe, and signal only, so
    new items reach a scored cluster / signal within the fast cadence. heavy=True
    adds the market-data work (baselines, grading, lab marking) and the
    attention/buzz rollup. Grading semantics are untouched (I12/I3): predictions
    still mature on their real horizon — heavy just runs the grader more often,
    it never fakes an early outcome."""
    sink = RawItemHandler(engine)

    async def _ingest() -> str:
        total = 0
        for src in sources:
            total += await run_source_once(src, sink)
        if total:
            publish_event("news", count=total)  # push: LIVE tape + badges refresh now
        return f"{total} new raw_items"

    _step("ingest", lambda: asyncio.run(_ingest()))

    with Session(engine) as s:
        cfg = get_or_create_config(s)

        def _enrich():
            stats = backfill_enrichment(s, resolver=resolver, tier_of=tiers.tier_of)
            return f"{stats.clusters} clusters"

        def _signal():
            preds = run_signal_cycle(
                s, cfg.params_json, cfg.config_version, provider=provider if market else None
            )
            if preds:
                publish_event("predictions", count=len(preds))
            return f"{len(preds)} predictions"

        def _grade():
            graded, still_open = grade_open_predictions(s, provider)
            if graded:
                publish_event("grades", count=graded)
            return f"graded={graded} open={still_open}"

        def _score():
            # Incremental by default on EVERY sweep: score only clusters with no
            # score row. Re-scoring the whole archive each full sweep was O(all
            # clusters x FinBERT) — ~10 min at 9K clusters, unbounded as the
            # archive grows. Taxonomy/config edits now require an explicit
            # `--rescore-all` pass to propagate to existing scores.
            n = score_clusters(s, finbert=finbert, lm=lm, only_unscored=not rescore_all)
            if n:
                publish_event("fired", count=n)  # newly scored clusters -> CATALYSTS refresh
            return f"{n} scored"

        def _mark():
            # Bound marking per sweep so a large open-observation backlog (e.g.
            # after downtime) can't monopolize the sweep and starve the fast
            # ingest->score->sim path behind it. Oldest-first; the backlog drains
            # across sweeps. Override via MARK_BUDGET env.
            budget = int(os.environ.get("MARK_BUDGET", "400"))
            marked, matured = mark_observations(s, provider, max_marks=budget)
            return f"marked={marked} matured={matured} (budget={budget})"

        def _attention():
            # Full idempotent recompute of attention_daily + buzz baselines each
            # cycle, folding the legacy archive in when present — build_attention_daily
            # DELETES and rebuilds from the engines given, so omitting legacy here
            # would wipe its history from the rollup.
            days = build_attention_daily(s, attention_engines or [engine])
            bases = compute_buzz_baselines(s)
            return f"{days} ticker-days, {bases} baselines"

        def _pm_calendar():
            # SPY-derived TradingCalendar for the premarket steps. ~45 days is
            # plenty (prev-trading-day + 2-day grading age); reaching today means
            # a 2h-TTL refetch at most — cheap inside the guarded windows.
            from datetime import timedelta as td

            from pipeline.common.timeutil import utcnow
            from pipeline.marketdata import TradingCalendar

            end = utcnow().date()
            spy = provider.get_benchmark_bars(end - td(days=45), end)
            return None if spy.empty else TradingCalendar.from_bars(spy)

        def _premarket():
            # PMR morning snapshot — freeze today's panel once, 08:30-09:30 ET.
            # ET-time gate runs FIRST so the other ~200 sweeps/day cost nothing.
            from pipeline.common.timeutil import utcnow
            from pipeline.panel import persist_premarket_snapshot
            from pipeline.panel.premarket import ET, FREEZE_ET, OPEN_ET

            now = utcnow()
            now_et = now.astimezone(ET)
            if not (FREEZE_ET <= now_et.time() < OPEN_ET) or now_et.weekday() >= 5:
                return "skipped (outside 08:30-09:30 ET)"
            cal = _pm_calendar()
            if cal is None:
                return "skipped (no benchmark bars)"
            # buzz boost intentionally omitted here (optional input; the panel
            # degrades gracefully) — rank stays fully deterministic from SQLite.
            return persist_premarket_snapshot(s, cal, now)

        def _premarket_grade():
            # PMR post-close report cards (>=16:30 ET gate lives in the function;
            # older ungraded panels grade on any full sweep). Pending-count gate
            # first so sweeps with nothing to grade never touch benchmark bars.
            from pipeline.common.models import PremarketPanel
            from pipeline.common.timeutil import utcnow
            from pipeline.panel import grade_premarket_panels

            pending = s.execute(
                select(func.count())
                .select_from(PremarketPanel)
                .where(PremarketPanel.graded_at.is_(None))
            ).scalar_one()
            if pending == 0:
                return "no pending panels"
            cal = _pm_calendar()
            if cal is None:
                return "skipped (no benchmark bars)"
            return grade_premarket_panels(s, provider, cal, utcnow())

        def _extended_fns():
            """(intraday_fn, daily_bars_fn) over the shared provider for the extended
            tracker — daily bars for robust regular-session prices, yfinance prepost
            for the best-effort pre/after-hours prints."""
            from pipeline.marketdata.intraday import intraday_bars

            def intraday_fn(t):
                return intraday_bars(t.upper(), "1d")

            def daily_bars_fn(t, start, end):
                df = provider.get_daily_bars(t, start, end)
                out = {}
                for _, row in df.iterrows():
                    d = row["date"].date() if hasattr(row["date"], "date") else row["date"]
                    out[d] = {"open": float(row["open"]), "close": float(row["adj_close"])}
                return out

            return intraday_fn, daily_bars_fn

        def _extended_premarket():
            # Log the day's PREMARKET behavior for the active/hot set, ~09:35-10:00 ET.
            # ET gate FIRST so the ~200 non-window sweeps cost nothing; intraday_bars'
            # 5-min TTL cache keeps the handful of in-window sweeps to ~one fetch each.
            from datetime import time as _t

            from pipeline.common.timeutil import utcnow
            from pipeline.marketdata.extended import (
                ET as _EXT_ET,
            )
            from pipeline.marketdata.extended import (
                active_extended_tickers,
                log_extended_session,
            )

            now = utcnow()
            now_et = now.astimezone(_EXT_ET)
            if now_et.weekday() >= 5 or not (_t(9, 35) <= now_et.time() < _t(10, 0)):
                return "skipped (outside 09:35-10:00 ET)"
            tickers = active_extended_tickers(s, now)
            if not tickers:
                return "no active tickers"
            intraday_fn, daily_bars_fn = _extended_fns()
            return log_extended_session(
                s,
                tickers,
                now=now,
                session_over=False,
                intraday_fn=intraday_fn,
                daily_bars_fn=daily_bars_fn,
            )

        def _extended_postmarket():
            # Log REGULAR close + AFTERHOURS for the day's tracked set, after the
            # 20:00 ET extended close (20:05-20:45 ET). reg_close comes from the daily
            # cache (final); ah_* from the day's prepost bars (same-day only).
            from datetime import time as _t

            from pipeline.common.timeutil import utcnow
            from pipeline.marketdata.extended import (
                ET as _EXT_ET,
            )
            from pipeline.marketdata.extended import (
                active_extended_tickers,
                log_extended_session,
            )

            now = utcnow()
            now_et = now.astimezone(_EXT_ET)
            if now_et.weekday() >= 5 or not (_t(20, 5) <= now_et.time() < _t(20, 45)):
                return "skipped (outside 20:05-20:45 ET)"
            tickers = active_extended_tickers(s, now)
            if not tickers:
                return "no active tickers"
            intraday_fn, daily_bars_fn = _extended_fns()
            return log_extended_session(
                s,
                tickers,
                now=now,
                session_over=True,
                intraday_fn=intraday_fn,
                daily_bars_fn=daily_bars_fn,
            )

        def _sim():
            # Paper-sim racing (Phase 2 rails). Master switch: SIM_ENABLED env,
            # default OFF; plus each config's own enabled flag. Quotes from
            # Alpaca latest trades — no quote means no fill, never fabricated.
            from pipeline.sim import run_sim_cycle, sim_enabled

            if not sim_enabled():
                return "disabled (SIM_ENABLED off)"
            from pipeline.marketdata.alpaca import AlpacaData, alpaca_configured

            if alpaca_configured():
                data = AlpacaData()

                def quote(t: str):
                    tr = data.latest_trade(t)
                    return tr.get("price") if tr else None
            else:

                def quote(t: str):
                    return None

            return run_sim_cycle(s, quote)

        _step("enrich", _enrich, session=s)
        _step("score", _score, session=s)
        _step("observe", lambda: f"{observe_scored_clusters(s)} observations", session=s)
        _step("roll_events", lambda: f"{roll_event_status(s)} passed", session=s)
        if market:
            _step("premarket", _premarket, session=s)
        _step("signal", _signal, session=s)
        _step("sim", _sim, session=s)
        if market and heavy:
            _step(
                "baselines",
                lambda: bool(emit_baselines(s, provider, cfg.config_version)),
                session=s,
            )
            _step("grade", _grade, session=s)
            _step("mark", _mark, session=s)
            _step("premarket_grade", _premarket_grade, session=s)
            _step("extended_premarket", _extended_premarket, session=s)
            _step("extended_postmarket", _extended_postmarket, session=s)
        if heavy:
            _step("attention", _attention, session=s)

        # Arm-time origin-news context: carry each new prediction's source_class /
        # headline / url into the companion table (LEDGER lanes). Incremental +
        # idempotent (only predictions missing a row), so it stays cheap per cycle.
        # After baselines so shadows inherit their real pred's origin the same cycle.
        _step(
            "context",
            lambda: f"{backfill_prediction_context(s)} contexts",
            session=s,
        )

        preds = s.execute(select(func.count()).select_from(Prediction)).scalar_one()
        obs = s.execute(select(func.count()).select_from(SignalObservation)).scalar_one()
        log.info("cycle done: ledger=%d predictions, lab=%d observations", preds, obs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None)
    parser.add_argument(
        "--interval",
        type=float,
        default=0.0,
        help="FULL-sweep loop seconds: baselines/grade/mark/attention cadence (0 = one cycle)",
    )
    parser.add_argument(
        "--fast-interval",
        type=float,
        default=float(os.environ.get("PIPELINE_FAST_INTERVAL", "120")),
        help="FAST deterministic sweep seconds (ingest/score/catalysts/signal only) "
        "between full sweeps (default: 120, or $PIPELINE_FAST_INTERVAL)",
    )
    parser.add_argument("--once", action="store_true", help="one cycle then exit")
    parser.add_argument(
        "--rescore-all",
        action="store_true",
        help="re-score EVERY cluster this run (required after editing "
        "configs/catalysts.yaml or scoring config; normal sweeps score only new clusters)",
    )
    parser.add_argument(
        "--no-finbert",
        action="store_true",
        help="hard override: LM-only scoring regardless of $SENTIMENT_MODE (low RAM)",
    )
    parser.add_argument(
        "--no-market", action="store_true", help="skip grade/mark/baselines (no yfinance)"
    )
    parser.add_argument(
        "--legacy",
        default=None,
        help="legacy SQLite path folded into the attention rollup "
        "(default: data/legacy.db when it exists)",
    )
    args = parser.parse_args()

    # Loud, self-explaining volume banner (log-only — the pipeline is NOT blocked;
    # phantom predictions on an ephemeral cutover DB are harmless, but this makes
    # future flaps obvious in the logs instead of a mystery). The TRADER driver,
    # which DOES place real orders, hard-refuses on the same signal.
    from pipeline.common.volume import volume_status

    _vs = volume_status(args.url)
    if _vs["on_railway"] and not _vs["persistent"]:
        log.warning(
            "=== EPHEMERAL CONTAINER — NO PERSISTENT VOLUME === pipeline writes here "
            "will NOT persist. %s (RAILWAY_VOLUME_MOUNT_PATH=%r). Likely a deploy "
            "cutover; the real container runs on the mounted volume.",
            _vs["reason"], _vs["railway_volume_path"],
        )
    elif _vs["on_railway"]:
        log.info("VOLUME OK (pipeline): %s", _vs["reason"])

    engine = make_engine(args.url)
    ensure_indexes(engine)  # backfill perf indexes onto a long-lived DB (idempotent)
    # Attention rollup sources: live DB + the legacy archive when present.
    legacy_path = Path(args.legacy) if args.legacy else _REPO / "data" / "legacy.db"
    attention_engines = [engine]
    if legacy_path.exists():
        attention_engines.append(make_engine(f"sqlite:///{legacy_path.as_posix()}"))
    tiers = load_source_tiers()
    provider = None if args.no_market else MarketDataProvider()
    lm = default_lm()
    # $SENTIMENT_MODE selects the FinBERT backend (lexicon | onnx | torch) with
    # automatic lexicon fallback; --no-finbert is a hard LM-only override.
    finbert = None if args.no_finbert else resolve_finbert()

    with Session(engine) as s:
        entities = s.execute(select(Entity)).scalars().all()
        if not entities:
            log.warning("entities table empty -- run seed_entities.py first (no attributions)")
        resolver = EntityResolver(entities)

    def one(heavy: bool = True) -> None:
        run_cycle(
            engine,
            resolver=resolver,
            tiers=tiers,
            provider=provider,
            lm=lm,
            finbert=finbert,
            sources=STRUCTURED_SOURCES + (SOCIAL_ARCHIVE_SOURCES if heavy else ()),
            market=not args.no_market,
            attention_engines=attention_engines,
            heavy=heavy,
            rescore_all=args.rescore_all,
        )

    if args.once or args.interval <= 0:
        one(heavy=True)
        return
    # Two-speed loop: FAST deterministic sweeps (ingest -> catalysts/score ->
    # signal, publishing push events) every fast_interval; a FULL sweep (adds
    # baselines/grade/mark/attention) whenever `interval` has elapsed. FinBERT
    # is loaded once above and reused across every sweep.
    fast = max(30.0, args.fast_interval)
    log.info(
        "pipeline loop: fast sweep every %.0fs, full sweep every %.0fs -- Ctrl-C to stop",
        fast,
        args.interval,
    )
    next_full = 0.0
    while True:
        heavy = time.monotonic() >= next_full
        log.info("=== %s sweep ===", "FULL" if heavy else "fast")
        # A fault outside a _step guard (e.g. get_or_create_config or the count
        # queries hitting a lock) must not crash the process — start.ps1 would
        # restart it, but each restart reloads FinBERT (~440MB) and a persistent
        # early fault becomes a crash-thrash loop. Skip the sweep instead.
        try:
            one(heavy=heavy)
        except Exception:  # noqa: BLE001 — one bad sweep must not kill the loop
            log.warning("sweep failed (continuing next cycle)", exc_info=True)
        if heavy:
            next_full = time.monotonic() + args.interval
        time.sleep(fast)


if __name__ == "__main__":
    main()
