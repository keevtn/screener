# OnnxFinbertAnalyzer

**Anchor:** `score/onnx_sentiment.py:61`

**Purpose:** The int8 ONNX FinBERT backend (~110MB): softmaxes the 3-class head, score = P(pos)-P(neg), labels banded at +/-0.05. Fetched to the volume at boot, self-tested before use.

**Receives from:** [[resolve_finbert]] — constructed only when SENTIMENT_MODE selects onnx.

**Feeds:** [[score_clusters]] — produces finbert_label/finbert_score per cluster.

*Stage: 03 Score*
