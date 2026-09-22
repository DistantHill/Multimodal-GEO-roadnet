"""CLI for bounded, read-only lineage comparison."""

from __future__ import annotations

import argparse
import json
from uuid import UUID

import psycopg

from ..validation.semantic_diff import compare_runs
from ._startup import load_startup_config


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a UUID") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare completed Pipeline lineages read-only.")
    parser.add_argument("--ask-password", action="store_true")
    parser.add_argument("--baseline-refine-id", type=_uuid, required=True)
    parser.add_argument("--baseline-detect-id", type=_uuid, required=True)
    parser.add_argument("--baseline-fit-id", type=_uuid, required=True)
    parser.add_argument("--candidate-refine-id", type=_uuid, required=True)
    parser.add_argument("--candidate-detect-id", type=_uuid, required=True)
    parser.add_argument("--candidate-fit-id", type=_uuid, required=True)
    args = parser.parse_args()
    config = load_startup_config(ask_password=args.ask_password)
    baseline = {kind: getattr(args, f"baseline_{kind}_id") for kind in ("refine", "detect", "fit")}
    candidate = {kind: getattr(args, f"candidate_{kind}_id") for kind in ("refine", "detect", "fit")}
    try:
        with psycopg.connect(config.postgres.dsn()) as conn:
            result = compare_runs(
                conn, baseline, candidate, batch_size=config.runtime.validation_batch_size,
            )
    except Exception as exc:
        result = {
            "version": 2, "passed": False, "persisted_equivalent": False,
            "complete_scope": False, "acceptance_passed": False,
            "network_parameterized_equivalent": None,
            "network_evidence": None, "network_tables": [],
            "error": type(exc).__name__,
            "scope": {"exclusions": [{
                "name": "comparison", "reason": "comparison failed",
                "blocks_complete_scope": True,
            }]},
            "tables": [],
        }
    print(json.dumps(result, allow_nan=False, ensure_ascii=False, sort_keys=True))
    return 0 if result["acceptance_passed"] else 1
