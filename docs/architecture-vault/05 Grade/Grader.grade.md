# Grader.grade

**Anchor:** `grade/grader.py:77`

**Purpose:** The grading rule: SPY defines the calendar, C0 is the clock-start close, r = (tk/tk[C0]-1) - (spy/spy[C0]-1) on adjusted closes (I10); first day |r|>=0.02 in the signed direction is correct, <=-0.02 incorrect, none by horizon expires; immature stays open.

**Receives from:** [[SignalEngine.evaluate|evaluate]] via [[predictions]] — grades open predictions on their real horizon.

**Receives from:** [[MarketDataProvider]] — reads SPY + ticker adjusted closes.

**Feeds:** [[apply_grade]] — hands the computed outcome to be written.

*Stage: 05 Grade*
