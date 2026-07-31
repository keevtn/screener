# default_client

**Anchor:** `agents/client.py:221`

**Purpose:** The injectable LLM client (CLAUDE_API switch): logs every call's spend and enforces a $2/day cap. The spine never imports it (I6) - agents propose, never decide (I13).

**Feeds:** [[run_ranking]] — the client the ranker calls.

**Feeds:** [[run_analyst]] — the client the analyst calls.

**Feeds:** [[run_deep_dive]] — the client the deep-dive calls.

**Feeds:** [[default_client]] via [[llm_spend]] — logs token spend and enforces the daily cap.

*Stage: 09 Agents*
