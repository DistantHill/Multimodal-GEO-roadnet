"""Explicit, audit-friendly reconciliation of immutable Parquet artifacts."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import sys
from typing import Sequence

from ..artifact_reconcile import apply_reconcile_plan, build_reconcile_plan
from ..config import PostgresConfig, connect as pg_connect, default_postgres_config
from ..config.pipeline_config import load_pipeline_config


_CRITICAL_KINDS = {"malformed_path", "malformed_registry", "registered_missing"}
_APPLY_REASON = "artifact reconcile CLI: expired failed or unregistered dataset cleanup"


def _nonnegative(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a nonnegative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("expected a nonnegative integer")
    return parsed


def _default_parquet_root() -> Path:
    if "PARQUET_ROOT" in os.environ:
        return Path(os.environ["PARQUET_ROOT"])
    return Path(os.getenv("DATA_ROOT", "/data")) / "parquet"


def _parser() -> argparse.ArgumentParser:
    defaults = default_postgres_config()
    parser = argparse.ArgumentParser(description="Reconcile immutable Parquet artifacts.")
    parser.add_argument("operation", choices=("reconcile",))
    parser.add_argument("--apply", action="store_true", help="Apply eligible removals; default is dry-run.")
    parser.add_argument("--parquet-root", type=Path, default=_default_parquet_root())
    parser.add_argument("--config", type=Path)
    parser.add_argument("--grace-days", type=_nonnegative)
    parser.add_argument("--host", default=defaults.host)
    parser.add_argument("--port", type=int, default=defaults.port)
    parser.add_argument("--database", default=defaults.database)
    parser.add_argument("--user", default=defaults.user)
    parser.add_argument("--ask-password", action="store_true")
    parser.add_argument("--connect-timeout", type=int, default=defaults.connect_timeout)
    parser.add_argument("--sslmode", default=defaults.sslmode)
    parser.set_defaults(_password=defaults.password)
    return parser


def _report(plan, mode: str) -> dict[str, object]:
    counts = Counter(item.kind for item in plan.actions)
    return {
        "mode": mode,
        "generated_at": plan.generated_at.isoformat(),
        "grace_days": plan.grace_days,
        "actions": [{
            "kind": item.kind,
            "path": item.relative_path,
            "eligible": item.eligible,
            "protected": item.protected,
            "reason": item.reason,
        } for item in plan.actions],
        "summary": {
            "total": len(plan.actions),
            "eligible": sum(item.eligible and not item.protected for item in plan.actions),
            "critical": sum(item.kind in _CRITICAL_KINDS for item in plan.actions),
            "by_kind": dict(sorted(counts.items())),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        grace_days = (args.grace_days if args.grace_days is not None else
                      load_pipeline_config(args.config).runtime.artifact_gc_grace_days)
        password = getpass.getpass("PostgreSQL password: ") if args.ask_password else args._password
        config = PostgresConfig(
            host=args.host, port=args.port, database=args.database, user=args.user,
            password=password, connect_timeout=args.connect_timeout, sslmode=args.sslmode,
        )
        conn = pg_connect(config)
        try:
            with conn.cursor() as cur:
                plan = build_reconcile_plan(
                    cur, args.parquet_root, datetime.now(timezone.utc), grace_days,
                )
                print(json.dumps(_report(plan, "apply" if args.apply else "dry-run"),
                                 sort_keys=True, separators=(",", ":")))
                if not args.apply:
                    conn.rollback()
                    return 0
                critical = [item for item in plan.actions if item.kind in _CRITICAL_KINDS]
                if critical:
                    conn.rollback()
                    print("Refusing --apply: reconciliation plan contains critical items.", file=sys.stderr)
                    return 2
                result = apply_reconcile_plan(
                    cur, args.parquet_root, plan, reason=_APPLY_REASON,
                )
            conn.commit()
            print(json.dumps({
                "applied": True,
                "removed_final": list(result.removed_final),
                "removed_staging": list(result.removed_staging),
                "removed_sidecars": list(result.removed_sidecars),
                "appended_events": result.appended_events,
            }, sort_keys=True, separators=(",", ":")))
            return 0
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception as exc:
        print(f"Artifact reconciliation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
