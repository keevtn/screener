"""LOCAL-ONLY: export + int8-quantize ProsusAI/finbert to ONNX for Railway.

Run this on your machine (or any box with the ML stack) — NEVER on Railway. The
heavy deps (torch/transformers/optimum) exist only here; Railway ships just
onnxruntime + tokenizers and consumes the artifacts this produces.

Install the export-only deps first (they are deliberately NOT in requirements.txt):

    pip install "optimum[onnxruntime]>=1.20" "transformers>=4.35" torch --extra-index-url https://download.pytorch.org/whl/cpu

Then:

    python scripts/export_finbert_onnx.py

Outputs:
  build/finbert-onnx/model.onnx        fp32 export (intermediate)
  build/finbert-onnx/model.int8.onnx   dynamic int8 — THE artifact to ship (~110 MB)
  models/finbert/tokenizer.json        committed tokenizer (small; the runtime reads this)
  models/finbert/vocab.txt, config.json, ...

It prints the int8 size + SHA256 and the exact Railway env vars to set.

CLI alternative (equivalent fp32 export, then quantize with this script's step 2):
    optimum-cli export onnx --model ProsusAI/finbert --task text-classification build/finbert-onnx/
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path

_TOKENIZER_FILES = (
    "tokenizer.json",
    "vocab.txt",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "config.json",
)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="ProsusAI/finbert", help="HF model id to export")
    ap.add_argument("--out", default="build/finbert-onnx", help="ONNX export dir (gitignored)")
    ap.add_argument(
        "--tokenizer-out",
        default="models/finbert",
        help="where to place the committed tokenizer (tokenizer.json etc.)",
    )
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # 1) Export to ONNX (fp32) via optimum.
    from optimum.onnxruntime import ORTModelForSequenceClassification
    from transformers import AutoTokenizer

    print(f"[export] exporting {args.model} -> ONNX (fp32)")
    model = ORTModelForSequenceClassification.from_pretrained(args.model, export=True)
    model.save_pretrained(out)
    AutoTokenizer.from_pretrained(args.model).save_pretrained(out)

    # 2) Dynamic int8 quantization (weights only — no calibration data needed).
    from onnxruntime.quantization import QuantType, quantize_dynamic

    fp32 = out / "model.onnx"
    int8 = out / "model.int8.onnx"
    print("[export] dynamic int8 quantization")
    quantize_dynamic(str(fp32), str(int8), weight_type=QuantType.QInt8)

    # 3) Copy the committable tokenizer files into the runtime tokenizer dir.
    tdir = Path(args.tokenizer_out)
    tdir.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in _TOKENIZER_FILES:
        src = out / name
        if src.exists():
            shutil.copy2(src, tdir / name)
            copied.append(name)

    # 4) Report + next steps.
    size_mb = int8.stat().st_size / 1024 / 1024
    digest = hashlib.sha256(int8.read_bytes()).hexdigest()
    print("\n[export] DONE")
    print(f"  int8 model : {int8}  ({size_mb:.1f} MB)")
    print(f"  sha256     : {digest}")
    print(f"  tokenizer  : {tdir}/  ({', '.join(copied)})")
    print("\nNext:")
    print(f"  1) commit the tokenizer:  git add {tdir}/tokenizer.json {tdir}/vocab.txt")
    print(f"  2) upload {int8.name} to a fetchable URL (e.g. a HuggingFace Hub repo you own)")
    print("  3) set Railway env vars on the app service:")
    print("       SENTIMENT_MODE=onnx")
    print("       FINBERT_ONNX_URL=<the upload URL>")
    print(f"       FINBERT_ONNX_SHA256={digest}")


if __name__ == "__main__":
    main()
