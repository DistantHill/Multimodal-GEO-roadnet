"""Replay one persisted ``track.v2`` result back to verified source rows.

ZH: 该 CLI 只解析目标和数据库连接参数，并输出可序列化的回放链路 JSON。
EN: This CLI only parses a target and connection options, then prints the
    serializable replay trace as JSON.

Usage / 用法::

    python -m track.v2.cli.replay --run-id <uuid> --kind intersection --id 42
"""

from __future__ import annotations

import argparse
import getpass

from ..config import PostgresConfig, connect as pg_connect, default_postgres_config
from ..output import error_payload, print_json
from ..replay import (
    PostgresReplayStore,
    PostgresSourceRowResolver,
    ReplayService,
    ReplayTarget,
)


def main() -> int:
    defaults = default_postgres_config()
    parser = argparse.ArgumentParser(
        description="逐级回放一个落盘 v2 实体到经校验的原始行 / Replay one persisted v2 entity to verified source rows."
    )
    parser.add_argument("--run-id", required=True, help="运行 UUID / Pipeline run UUID.")
    parser.add_argument("--kind", required=True, help="落盘实体类型 / Persisted entity kind.")
    parser.add_argument("--id", required=True, type=int, help="数据库主键 / Database primary key.")
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
            service = ReplayService(
                PostgresReplayStore(conn),
                source_row_resolver=PostgresSourceRowResolver(conn),
            )
            trace = service.replay(ReplayTarget(args.kind, args.id, args.run_id))
    except (RuntimeError, ValueError) as exc:
        print_json(error_payload("invalid_request", str(exc)))
        return 2
    except Exception as exc:
        print_json(error_payload("unavailable", str(exc)))
        return 1

    print_json(trace.as_dict())
    return 0 if trace.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
