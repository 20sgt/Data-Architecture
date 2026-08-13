#!/usr/bin/env python3
"""CLI: free keyword RAG over podcast silver + recent gold matters."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow `python rag/query.py` from package root as well as `python -m rag.query`.
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))

from rag.retrieve import retrieve  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Retrieve up to 3 podcast citation chunks (quote, title, URL) "
            "using word overlap with the query and last-N-days gold matters. "
            "No paid APIs."
        )
    )
    parser.add_argument("query", help="Natural language question")
    parser.add_argument(
        "--matters",
        default=None,
        help="Path to gold matters JSON (default: rag/data/matters.json or example)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to podcast_silver.sqlite",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=28,
        help="Gold matters lookback window (default 28)",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="YYYY-MM-DD anchor date for the window (default: today)",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=3,
        help="Max chunks to return (default 3)",
    )
    args = parser.parse_args(argv)

    try:
        result = retrieve(
            args.query,
            top_k=args.top_k,
            window_days=args.window_days,
            as_of=args.as_of,
            matters_path=args.matters,
            sqlite_path=args.db,
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("status") == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
