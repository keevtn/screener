"""Boot-time fetch of the int8 ONNX FinBERT onto the Railway volume (cached).

The quantized model (~110 MB) is too big for the 100 MB GitHub limit, so it is
NOT committed — it is downloaded once to the volume and reused across restarts.

No-op unless SENTIMENT_MODE=onnx AND FINBERT_ONNX_URL is set. Fully fail-soft:
any problem (no URL, network error, checksum mismatch) logs and exits 0, so boot
continues and scoring falls back to lexicon-only (resolve_finbert() sees no model).

Env:
  SENTIMENT_MODE       must be 'onnx' for this to do anything
  FINBERT_ONNX_URL     download source (e.g. a HuggingFace Hub resolve URL)
  FINBERT_ONNX_PATH    volume destination (default data/models/finbert-int8.onnx)
  FINBERT_ONNX_SHA256  optional integrity check; on mismatch the file is discarded
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path


def _log(msg: str) -> None:
    print(f"[fetch_model] {msg}", flush=True)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    mode = os.environ.get("SENTIMENT_MODE", "lexicon").strip().lower()
    if mode not in ("onnx", "onnx-int8", "quantized"):
        _log(f"SENTIMENT_MODE={mode!r} (not onnx); nothing to fetch")
        return 0

    url = os.environ.get("FINBERT_ONNX_URL")
    dest = Path(os.environ.get("FINBERT_ONNX_PATH", "data/models/finbert-int8.onnx"))
    want = os.environ.get("FINBERT_ONNX_SHA256")

    if dest.exists() and (not want or _sha256(dest) == want.lower()):
        _log(f"model present at {dest} ({dest.stat().st_size / 1024 / 1024:.1f} MB); cached")
        return 0
    if not url:
        _log("SENTIMENT_MODE=onnx but FINBERT_ONNX_URL unset; skipping (stays lexicon-only)")
        return 0

    try:
        import httpx  # already a runtime dep

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        _log(f"downloading {url} -> {dest}")
        h = hashlib.sha256()
        size = 0
        with httpx.stream("GET", url, follow_redirects=True, timeout=180) as r:
            r.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
        if want and h.hexdigest() != want.lower():
            tmp.unlink(missing_ok=True)
            _log(f"checksum mismatch (got {h.hexdigest()}, want {want}); discarded → lexicon-only")
            return 0
        tmp.replace(dest)
        _log(f"fetched {size / 1024 / 1024:.1f} MB, sha256={h.hexdigest()}")
    except Exception as exc:  # noqa: BLE001 — never block boot on a model fetch
        _log(f"download failed ({exc}); continuing lexicon-only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
