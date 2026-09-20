"""CLI for bounded, read-only lineage comparison."""

from __future__ import annotations

import argparse
import json
import os
from uuid import UUID

import psycopg

from ..validation.semantic_diff import compare_runs


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a UUID") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare completed Pipeline lineages read-only.")
    parser.add_argument("--dsn", default=os.getenv("ROADNET_DIFF_DSN"))
    parser.add_argument("--baseline-refine-id", type=_uuid, required=True)
    parser.add_argument("--baseline-detect-id", type=_uuid, required=True)
    parser.add_argument("--baseline-fit-id", type=_uuid, required=True)
    parser.add_argument("--candidate-refine-id", type=_uuid, required=True)
    parser.add_argument("--candidate-detect-id", type=_uuid, required=True)
    parser.add_argument("--candidate-fit-id", type=_uuid, required=True)
    parser.add_argument("--batch-size", type=int, default=2000)
    args = parser.parse_args()
    if not args.dsn:
        parser.error("--dsn or ROADNET_DIFF_DSN is required")
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    baseline = {kind: getattr(args, f"baseline_{kind}_id") for kind in ("refine", "detect", "fit")}
    candidate = {kind: getattr(args, f"candidate_{kind}_id") for kind in ("refine", "detect", "fit")}
    try:
        with psycopg.connect(args.dsn) as conn:
            result = compare_runs(conn, baseline, candidate, batch_size=args.batch_size)
    except Exception as exc:
        result = {"version": 1, "passed": False, "error": type(exc).__name__, "tables": []}
    print(json.dumps(result, allow_nan=False, ensure_ascii=False, sort_keys=True))
    return 0 if result["passed"] else 1
