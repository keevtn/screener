# resolve_armed_state

**Anchor:** `signal/armed.py:128`

**Purpose:** Resolves an armed state: expire after 96h with no bars, no_signal if |reaction|<0.02, else emit a prediction with direction=sign(reaction), confidence=min(1,0.5+|r|*5).

**Receives from:** [[arm_reaction_dependent]] via [[armed_states]] — reads the armed rows.

**Receives from:** [[market_adjusted_reaction]] — signs the prediction from the realized move because text couldn't.

**Feeds:** [[resolve_armed_state]] via [[predictions]] — writes the PEAD prediction.

**Feeds:** [[run_signal_cycle]] — returns resolutions to the cycle.

*Stage: 04 Signal*
