"""Explicit, audit-friendly reconciliation of immutable Parquet artifacts."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import sys
from typing import Sequence

from ..artifacts.reconcile_datasets import apply_reconcile_plan, build_reconcile_plan
from ..config import connect as pg_connect
from ._load_config import load_startup_config


_CRITICAL_KINDS = {"malformed_path", "malformed_registry", "registered_missing"}
_APPLY_REASON = "artifact reconcile CLI: expired failed or unregistered dataset cleanup"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reconcile immutable Parquet artifacts.")
    parser.add_argument("operation", choices=("reconcile",))
    parser.add_argument("--apply", action="store_true", help="Apply eligible removals; default is dry-run.")
    parser.add_argument("--ask-password", action="store_true")
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
        pipeline_config = load_startup_config(ask_password=args.ask_password)
        grace_days = pipeline_config.runtime.artifact_gc_grace_days
        config = pipeline_config.postgres
        parquet_root = pipeline_config.paths.parquet_directory
        conn = pg_connect(config)
        try:
            with conn.cursor() as cur:
                plan = build_reconcile_plan(
                    cur, parquet_root, datetime.now(timezone.utc), grace_days,
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
                    cur, parquet_root, plan, reason=_APPLY_REASON,
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
