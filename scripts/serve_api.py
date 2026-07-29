"""Serve the read-only API (docs/ROADMAP.md task 5.4).

    python scripts/serve_api.py [--host 127.0.0.1] [--port 8001]

Uses DATABASE_URL from the environment (default sqlite:///data/pipeline.db).

Host/port default to the HOST/PORT env vars when set, falling back to
127.0.0.1:8001 for local dev. On Railway the platform injects PORT and the
service must bind 0.0.0.0, so the container start command sets HOST=0.0.0.0 and
this picks up the assigned PORT with no code change.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

import uvicorn

from pipeline.api import create_app
from pipeline.common.db import make_engine


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8001))
    args = parser.parse_args()
    # The API shares pipeline.db with the pipeline loop; a heavy sweep can hold
    # the write lock past the 5s default, turning a force-run write into an
    # unhandled "database is locked" 500. 30s lets it wait the burst out.
    engine = make_engine(busy_timeout_ms=30000)
    uvicorn.run(create_app(engine), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
