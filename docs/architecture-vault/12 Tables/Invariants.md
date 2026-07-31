# Invariants

**Anchor:** `models.py + docs`

**Purpose:** The 13 numbered invariants (I1-I13) the whole system is built to preserve: e.g. I2 raw_items append-only, I4 predictions grader-fields-only, I5 one story scored once, I7 sentiment axes separate, I8 structured-only signal, I10 adjusted-close trading days, I12 no lookahead.

**Feeds:** [[scorer_visible_stmt]] — I8 is enforced here.

**Feeds:** [[apply_grade]] — I4 restricts writable fields.

**Feeds:** [[SignalEngine.build_window|build_window]] — I8 structured-only.

**Feeds:** [[market_adjusted_reaction]] — I12 no lookahead.

**Feeds:** [[WindowAccumulator]] — I7 blend only at aggregation.

*Stage: 12 Tables*
