# fetch_model

**Anchor:** `fetch_model.py`

**Purpose:** Downloads the sha256-pinned ONNX FinBERT to the volume at boot; a hash mismatch is discarded and scoring falls back to lexicon-only.

**Receives from:** [[railway_start]] — invoked during bootstrap.

**Feeds:** [[OnnxFinbertAnalyzer]] — provides the pinned int8 model file.

*Stage: 13 Ops*
