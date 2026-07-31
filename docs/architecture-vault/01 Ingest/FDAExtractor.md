# FDAExtractor

**Anchor:** `backend/IngestionModule.py:1025`

**Purpose:** Polls FDA press-release RSS, MedWatch safety alerts, and openFDA enforcement JSON (drug/device/food recalls) into structured NewsItems.

**Receives from:** [[_HttpClient]] — uses the shared client.

**Feeds:** [[run_source_once]] — yields source_type=fda items feeding fda_action catalysts.

*Stage: 01 Ingest*
