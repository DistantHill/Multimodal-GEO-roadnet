"""Replay one persisted schema-v4 result back to verified source rows.

Refine artifacts use ``--refine-id``; Detect artifacts use ``--detect-id``;
Centerlines carry their own completed Fit/Detect context.
"""

from __future__ import annotations

import argparse
import uuid

from ..config import connect as pg_connect
from ..db.schema import require_current_schema
from ..telemetry.console_logging import init_logger, shutdown_logger
from .json_output import error_payload, print_json
from ..validation.replay_lineage import (
    REFINE_REPLAY_KINDS,
    DETECT_REPLAY_KINDS,
    PostgresReplayStore,
    ReplayService,
    ReplayTarget,
    replay_centerline_inputs,
)
from ._load_config import load_startup_config


def _uuid_arg(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a UUID") from exc


def _target_from_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> ReplayTarget:
    if args.kind == "centerline":
        if args.refine_id or args.detect_id:
            parser.error("centerline does not take a stage ID")
        return ReplayTarget(args.kind, _internal_id(parser, args.id))
    if args.kind in REFINE_REPLAY_KINDS:
        if args.refine_id is None:
            parser.error(f"{args.kind} requires --refine-id")
        target_id: int | str = (
            args.id
            if args.kind in {"source_point", "excluded_point"}
            else _internal_id(parser, args.id)
        )
        return ReplayTarget(args.kind, target_id, refine_id=args.refine_id)
    if args.kind in DETECT_REPLAY_KINDS:
        if args.detect_id is None:
            parser.error(f"{args.kind} requires --detect-id")
        return ReplayTarget(
            args.kind,
            _internal_id(parser, args.id),
            detect_id=args.detect_id,
        )
    parser.error(f"unsupported replay kind: {args.kind}")
    raise AssertionError("argparse.error always exits")


def _internal_id(parser: argparse.ArgumentParser, value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        parser.error("this replay kind requires an integer database ID")
    if result <= 0:
        parser.error("database ID must be positive")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay one persisted schema-v4 entity to verified source rows."
    )
    stage_group = parser.add_mutually_exclusive_group()
    stage_group.add_argument("--refine-id", type=_uuid_arg, help="Refine Run UUID.")
    stage_group.add_argument("--detect-id", type=_uuid_arg, help="Detect Run UUID.")
    parser.add_argument("--kind", required=True, help="Persisted entity kind.")
    parser.add_argument(
        "--id",
        required=True,
        help="Internal database ID, or source_row_key for source/excluded points.",
    )
    parser.add_argument("--ask-password", action="store_true")
    args = parser.parse_args()
    target = _target_from_args(parser, args)

    pipeline_config = load_startup_config(ask_password=args.ask_password)
    config = pipeline_config.postgres
    init_logger(pipeline_config.logging.directory)
    try:
        try:
            with pg_connect(config) as conn:
                require_current_schema(conn)
                service = ReplayService(PostgresReplayStore(conn))
                trace = service.replay(target)
        except (RuntimeError, ValueError) as exc:
            print_json(error_payload("invalid_request", str(exc)))
            return 2
        except Exception as exc:
            print_json(error_payload("unavailable", str(exc)))
            return 1

        print_json(trace.as_dict())
        return 0 if trace.status == "ok" else 1
    finally:
        shutdown_logger()


if __name__ == "__main__":
    raise SystemExit(main())
