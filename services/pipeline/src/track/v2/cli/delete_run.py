"""Delete one Refine or Detect Run after preview and two confirmations."""

from __future__ import annotations

import argparse
import uuid

from ..config import connect as pg_connect
from ..delete_run import DeleteRunService
from ..telemetry.console_logging import init_logger, shutdown_logger
from ._load_config import load_startup_config


def _uuid_arg(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a UUID") from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preview and explicitly delete one Refine or Detect Run."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--refine-id", type=_uuid_arg)
    target.add_argument("--detect-id", type=_uuid_arg)
    parser.add_argument("--ask-password", action="store_true")
    args = parser.parse_args()

    pipeline_config = load_startup_config(ask_password=args.ask_password)
    config = pipeline_config.postgres
    init_logger(pipeline_config.logging.directory)
    try:
        try:
            with pg_connect(config) as conn:
                service = DeleteRunService(conn)
                deleted = (
                    service.confirm_and_delete_refine(args.refine_id)
                    if args.refine_id
                    else service.confirm_and_delete_detect(args.detect_id)
                )
        except (RuntimeError, ValueError) as exc:
            print(f"Error: {exc}")
            return 2
        return 0 if deleted else 1
    finally:
        shutdown_logger()


if __name__ == "__main__":
    raise SystemExit(main())
