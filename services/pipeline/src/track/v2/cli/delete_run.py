"""Delete one Clean or Detect Run after preview and two confirmations."""

from __future__ import annotations

import argparse
import getpass
import uuid

from ..config import PostgresConfig, connect as pg_connect, default_postgres_config
from ..delete_run import DeleteRunService


def _uuid_arg(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a UUID") from exc


def main() -> int:
    defaults = default_postgres_config()
    parser = argparse.ArgumentParser(
        description="Preview and explicitly delete one Clean or Detect Run."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--clean-id", type=_uuid_arg)
    target.add_argument("--detect-id", type=_uuid_arg)
    parser.add_argument("--host", default=defaults.host)
    parser.add_argument("--port", type=int, default=defaults.port)
    parser.add_argument("--database", default=defaults.database)
    parser.add_argument("--user", default=defaults.user)
    parser.add_argument("--ask-password", action="store_true")
    parser.add_argument("--connect-timeout", type=int, default=defaults.connect_timeout)
    parser.add_argument("--sslmode", default=defaults.sslmode)
    args = parser.parse_args()

    config = PostgresConfig(
        host=args.host,
        port=args.port,
        database=args.database,
        user=args.user,
        password=getpass.getpass("PostgreSQL password: ") if args.ask_password else defaults.password,
        connect_timeout=args.connect_timeout,
        sslmode=args.sslmode,
    )
    try:
        with pg_connect(config) as conn:
            service = DeleteRunService(conn)
            deleted = (
                service.confirm_and_delete_clean(args.clean_id)
                if args.clean_id
                else service.confirm_and_delete_detect(args.detect_id)
            )
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}")
        return 2
    return 0 if deleted else 1


if __name__ == "__main__":
    raise SystemExit(main())
