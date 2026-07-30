"""Boot-time self-heal of column-shifted fundamentals_snapshots rows.

Verifies the detect signature is specific (never fires on healthy data), that a
repair wipes ONLY fundamentals_snapshots, and that the subsequent name-based
re-copy from the seed restores real numeric prices.
"""

from __future__ import annotations

import sqlite3

from scripts.hydrate_seed import _repair_shifted_fundamentals


def _seed_conn(tmp_path):
    """A seed DB with fundamentals_snapshots in the SOURCE physical order
    (price/change_pct AFTER created_at) — the layout that caused the shift — plus
    a couple of unrelated tables that must never be touched by the repair."""
    p = tmp_path / "seed.db"
    c = sqlite3.connect(p)
    c.execute(
        "CREATE TABLE fundamentals_snapshots "
        "(ticker TEXT, as_of TEXT, created_at TEXT, price REAL, change_pct REAL)"
    )
    c.executemany(
        "INSERT INTO fundamentals_snapshots VALUES (?,?,?,?,?)",
        [("NVDA", "2026-07-22", "2026-07-22 14:21:40.9", 207.29, 0.05),
         ("AAPL", "2026-07-22", "2026-07-22 14:21:40.9", 231.10, 0.01)],
    )
    c.execute("CREATE TABLE predictions (prediction_id TEXT, ticker TEXT)")
    c.execute("INSERT INTO predictions VALUES ('p1','NVDA')")
    c.commit()
    c.close()
    return p


def _live_conn(tmp_path, *, corrupt: bool):
    """A live DB with fundamentals_snapshots in MODEL order (price/change_pct
    BEFORE created_at). When corrupt=True, price holds a shifted timestamp."""
    p = tmp_path / "live.db"
    c = sqlite3.connect(p)
    c.execute(
        "CREATE TABLE fundamentals_snapshots "
        "(ticker TEXT, as_of TEXT, price REAL, change_pct REAL, created_at TEXT)"
    )
    c.execute("CREATE TABLE predictions (prediction_id TEXT, ticker TEXT)")
    c.execute("INSERT INTO predictions VALUES ('live1','TSLA')")  # live history to protect
    if corrupt:
        # the shifted state: created_at timestamp in price, real price in change_pct
        c.execute(
            "INSERT INTO fundamentals_snapshots VALUES "
            "('NVDA','2026-07-22','2026-07-22 14:21:40.9',207.29,'2026-07-22 14:21:40.9')"
        )
    else:
        c.execute("INSERT INTO fundamentals_snapshots VALUES ('NVDA','2026-07-22',207.29,0.05,'2026-07-22 14:21:40.9')")
    c.commit()
    c.close()
    return p


def _attach(live_path, seed_path):
    con = sqlite3.connect(live_path)
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute(f"ATTACH DATABASE '{seed_path.as_posix()}' AS seed")
    return con


def test_detects_and_wipes_only_fundamentals(tmp_path):
    con = _attach(_live_conn(tmp_path, corrupt=True), _seed_conn(tmp_path))
    # sanity: the corrupt row is text in a REAL column
    assert con.execute("SELECT typeof(price) FROM fundamentals_snapshots").fetchone()[0] == "text"

    repaired = _repair_shifted_fundamentals(con)
    assert repaired == 1
    # ONLY fundamentals_snapshots was wiped; predictions (live history) is untouched
    assert con.execute("SELECT COUNT(*) FROM fundamentals_snapshots").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 1
    con.close()


def test_no_op_on_healthy_data(tmp_path):
    con = _attach(_live_conn(tmp_path, corrupt=False), _seed_conn(tmp_path))
    repaired = _repair_shifted_fundamentals(con)
    assert repaired == 0  # signature must never fire on healthy numeric prices
    assert con.execute("SELECT COUNT(*) FROM fundamentals_snapshots").fetchone()[0] == 1
    con.close()


def test_repair_then_name_copy_restores_real_prices(tmp_path):
    con = _attach(_live_conn(tmp_path, corrupt=True), _seed_conn(tmp_path))
    _repair_shifted_fundamentals(con)  # wipes the corrupt rows
    # re-copy by column name (the same logic the hydrate loop runs post-wipe)
    main_cols = [r[1] for r in con.execute('PRAGMA main.table_info("fundamentals_snapshots")').fetchall()]
    seed_cols = {r[1] for r in con.execute('PRAGMA seed.table_info("fundamentals_snapshots")').fetchall()}
    cols = [c for c in main_cols if c in seed_cols]
    cl = ", ".join(f'"{c}"' for c in cols)
    con.execute(f'INSERT OR IGNORE INTO main."fundamentals_snapshots" ({cl}) SELECT {cl} FROM seed."fundamentals_snapshots"')

    rows = con.execute(
        "SELECT ticker, price, typeof(price), change_pct FROM fundamentals_snapshots ORDER BY ticker"
    ).fetchall()
    assert len(rows) == 2
    for ticker, price, tp, change in rows:
        assert tp == "real"          # price is a real number again
        assert price > 100           # a plausible price, not a timestamp/percent
        assert -1.0 < change < 1.0   # change_pct is a fraction again
    con.close()


def test_no_op_when_table_absent(tmp_path):
    # live DB without the table at all -> nothing to repair, no error
    live = tmp_path / "bare.db"
    c = sqlite3.connect(live)
    c.execute("CREATE TABLE predictions (prediction_id TEXT)")
    c.commit()
    c.close()
    con = _attach(live, _seed_conn(tmp_path))
    assert _repair_shifted_fundamentals(con) == 0
    con.close()
