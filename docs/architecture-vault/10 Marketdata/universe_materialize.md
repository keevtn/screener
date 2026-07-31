# universe_materialize

**Anchor:** `marketdata/universe.py:79`

**Purpose:** Materializes the tradable universe into universe_snapshots for the screener and fundamentals endpoints.

**Receives from:** [[SymbolDirectoryProvider]] — reads listing metadata.

**Feeds:** [[universe_materialize]] via [[universe_snapshots]] — writes the universe snapshot.

**Feeds:** [[screener_rows]] via [[universe_snapshots]] — the screener reads the snapshot.

*Stage: 10 Marketdata*
