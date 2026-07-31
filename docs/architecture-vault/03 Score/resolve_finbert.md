# resolve_finbert

**Anchor:** `score/sentiment.py:90`

**Purpose:** Resolves the sentiment ladder by SENTIMENT_MODE: onnx (with a live self-test) / torch / lexicon-only, degrading to LM-only on any failure. FinBERT and Loughran-McDonald scores are stored separately (I7).

**Feeds:** [[score_clusters]] — supplies the chosen analyzer.

**Feeds:** [[OnnxFinbertAnalyzer]] — instantiates the int8 backend when selected.

**Feeds:** [[resolve_finbert]] via [[finbert_status]] — writes backend health read by /health.

*Stage: 03 Score*
