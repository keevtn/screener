# AlpacaPaperBroker

**Anchor:** `sim/broker.py:96`

**Purpose:** The ONLY order path. __init__ hard-asserts the paper URL (BrokerGuardrailError), submits whole-share DAY market orders, reconciles fills only on 'filled'; NOTIONAL 1000, MAX_OPEN 25, per-run entry cap 40 (closes exempt).

**Receives from:** [[evaluate_entries]] — receives entry orders.

**Receives from:** [[decide_exit]] — receives exit orders.

**Feeds:** [[AlpacaPaperBroker]] via [[sim_trades]] — records broker order ids for provenance.

**Feeds:** [[pair_round_trips]] via [[sim_trades]] — fills flow into the FIFO lot matcher.

*Stage: 07 Trader*
