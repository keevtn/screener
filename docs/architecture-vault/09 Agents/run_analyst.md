# run_analyst

**Anchor:** `agents/analyst.py:120`

**Purpose:** Weekly analyst that proposes config/taxonomy changes as pending_changes (FROZEN_PATHS stripped); a human must approve before anything lands (I3, I13).

**Receives from:** [[default_client]] — uses the LLM client.

**Feeds:** [[api_app]] — proposals surface for human approval.

*Stage: 09 Agents*
