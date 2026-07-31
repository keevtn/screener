# dual_layer_immutability

**Anchor:** `models.py:677-699`

**Purpose:** The immutability machinery: ORM before-flush hooks plus SQL triggers that block edits to append-only / grader-only columns, so invariants are enforced in the database, not by convention.

**Feeds:** [[apply_grade]] via [[predictions]] — blocks writes outside the grader fields.

**Feeds:** [[RawItemHandler.write|write]] via [[raw_items]] — keeps raw_items append-only.

*Stage: 12 Tables*
