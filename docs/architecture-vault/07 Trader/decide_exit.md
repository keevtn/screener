# decide_exit

**Anchor:** `sim/exitpolicy.py:90`

**Purpose:** Exit-policy engine over {horizon_hold, stop, bracket, trailing_after_threshold, time_decay, vol_stop}; horizon is always the backstop, vol_stop exits when favorable move <= -2.0*atr_frac. Policies are content-addressed; live A/B runs 4 vs 4.

**Receives from:** [[evaluate_entries]] via [[sim_trades]] — evaluates open positions.

**Receives from:** [[vol.atr_fraction|atr_fraction]] — vol_stop needs the ATR fraction.

**Feeds:** [[AlpacaPaperBroker]] — submits the exit when a policy triggers.

**Feeds:** [[EOD_flatten]] via [[sim_trades]] — the exit closes the position.

*Stage: 07 Trader*
