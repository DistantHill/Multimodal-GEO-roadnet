"""Replay one persisted v2 result back to verified source rows.

Clean artifacts use ``--clean-id``; Detect artifacts use ``--detect-id``;
``trajectory_point`` has no stage identifier.
"""

from __future__ import annotations

import argparse
import getpass
import uuid

from ..config import PostgresConfig, connect as pg_connect, default_postgres_config
from ..logger import init_logger, shutdown_logger
from ..output import error_payload, print_json
from ..replay import (
    CLEAN_REPLAY_KINDS,
    DETECT_REPLAY_KINDS,
    PostgresReplayStore,
    PostgresSourceRowResolver,
    ReplayService,
    ReplayTarget,
)


def _uuid_arg(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a UUID") from exc


def _target_from_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> ReplayTarget:
    if args.kind == "trajectory_point":
        if args.clean_id or args.detect_id:
            parser.error("trajectory_point does not take a stage ID")
        return ReplayTarget(args.kind, args.id)
    if args.kind in CLEAN_REPLAY_KINDS:
        if args.clean_id is None:
            parser.error(f"{args.kind} requires --clean-id")
        return ReplayTarget(args.kind, args.id, clean_id=args.clean_id)
    if args.kind in DETECT_REPLAY_KINDS:
        if args.detect_id is None:
            parser.error(f"{args.kind} requires --detect-id")
        return ReplayTarget(args.kind, args.id, detect_id=args.detect_id)
    parser.error(f"unsupported replay kind: {args.kind}")
    raise AssertionError("argparse.error always exits")


def main() -> int:
    defaults = default_postgres_config()
    parser = argparse.ArgumentParser(
        description="Replay one persisted v2 entity to verified source rows."
    )
    stage_group = parser.add_mutually_exclusive_group()
    stage_group.add_argument("--clean-id", type=_uuid_arg, help="Clean Run UUID.")
    stage_group.add_argument("--detect-id", type=_uuid_arg, help="Detect Run UUID.")
    parser.add_argument("--kind", required=True, help="Persisted entity kind.")
    parser.add_argument("--id", required=True, type=int, help="Database primary key.")
    parser.add_argument("--host", default=defaults.host)
    parser.add_argument("--port", type=int, default=defaults.port)
    parser.add_argument("--database", default=defaults.database)
    parser.add_argument("--user", default=defaults.user)
    parser.add_argument("--ask-password", action="store_true")
    parser.add_argument("--connect-timeout", type=int, default=defaults.connect_timeout)
    parser.add_argument("--sslmode", default=defaults.sslmode)
    args = parser.parse_args()
    target = _target_from_args(parser, args)

    config = PostgresConfig(
        host=args.host,
        port=args.port,
        database=args.database,
        user=args.user,
        password=getpass.getpass("PostgreSQL password: ") if args.ask_password else defaults.password,
        connect_timeout=args.connect_timeout,
        sslmode=args.sslmode,
    )
    init_logger()
    try:
        try:
            with pg_connect(config) as conn:
                service = ReplayService(
                    PostgresReplayStore(conn),
                    source_row_resolver=PostgresSourceRowResolver(conn),
                )
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
