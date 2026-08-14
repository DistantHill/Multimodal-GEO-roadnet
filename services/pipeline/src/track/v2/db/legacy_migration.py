"""Deterministic migration from the legacy pipeline run registry."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class LegacyPipelineRun:
    id: str
    source_id: str
    source_kind: str
    parameters: dict[str, Any]
    status: str
    started_at: Any
    finished_at: Any


@dataclass(frozen=True, slots=True)
class MigratedDetectRun:
    id: str
    clean_id: str
    parameters: dict[str, Any]
    status: str
    started_at: Any
    finished_at: Any


@dataclass(frozen=True, slots=True)
class LegacyMigrationPlan:
    clean_runs: tuple[LegacyPipelineRun, ...]
    detect_runs: tuple[MigratedDetectRun, ...]


def load_legacy_clean_map(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("legacy Clean map must be a JSON object")
    result: dict[str, str] = {}
    for detect_id, clean_id in raw.items():
        try:
            result[str(uuid.UUID(str(detect_id)))] = str(uuid.UUID(str(clean_id)))
        except ValueError as exc:
            raise ValueError("legacy Clean map keys and values must be UUIDs") from exc
    return result


def build_legacy_migration_plan(
    runs: list[LegacyPipelineRun],
    legacy_clean_map: dict[str, str],
) -> LegacyMigrationPlan:
    legacy_ids = {run.id for run in runs}
    inferred_clean_ids = {
        run.id for run in runs
        if run.parameters.get("skip_clean") is False
    }
    ambiguous_ids = [
        run.id for run in runs
        if run.parameters.get("skip_clean") is not False
    ]
    missing = [run_id for run_id in ambiguous_ids if run_id not in legacy_clean_map]
    if missing:
        raise ValueError(
            "ambiguous legacy pipeline_runs require --legacy-clean-map: "
            + ", ".join(missing)
        )
    mapped_unknown = sorted(set(legacy_clean_map) - set(ambiguous_ids))
    if mapped_unknown:
        raise ValueError(
            "legacy Clean map contains non-ambiguous Detect Run ids: "
            + ", ".join(mapped_unknown)
        )
    unavailable = sorted(set(legacy_clean_map.values()) - legacy_ids)
    if unavailable:
        raise ValueError(
            "legacy Clean map must reference a real legacy run: "
            + ", ".join(unavailable)
        )

    clean_ids = inferred_clean_ids | set(legacy_clean_map.values())
    clean_runs = [run for run in runs if run.id in clean_ids]

    detects = tuple(
        MigratedDetectRun(
            id=run.id,
            clean_id=(
                run.id if run.id in inferred_clean_ids
                else legacy_clean_map[run.id]
            ),
            parameters=run.parameters,
            status=run.status,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )
        for run in runs
    )
    return LegacyMigrationPlan(tuple(clean_runs), detects)


_CLEAN_STAGE_TABLES = (
    "excluded_points", "kept_trajectory_points", "injected_points",
)
_DETECT_STAGE_TABLES = (
    "tracks_cross", "turn_events", "candidate_intersections",
    "candidate_intersection_events", "candidate_entry_exit_points",
    "intersections", "entry_exit_points",
)


def migrate_legacy_pipeline_runs(
    cur: Any,
    legacy_clean_map: dict[str, str],
    *,
    registry_ddl: Mapping[str, str],
    index_statements: Sequence[tuple[str, str]],
) -> bool:
    """Atomically split a legacy ``pipeline_runs`` registry.

    The caller owns the transaction. Ambiguity is resolved before the first
    mutating statement; every later error therefore rolls back the full split.
    Returns ``False`` when the legacy registry no longer exists.
    """
    cur.execute("SELECT to_regclass('pipeline_runs')")
    if cur.fetchone()[0] is None:
        return False

    for table in (
        "pipeline_runs", *_CLEAN_STAGE_TABLES, *_DETECT_STAGE_TABLES,
    ):
        cur.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")

    cur.execute(
        "SELECT id, source_id, source_kind, parameters, status, started_at, finished_at "
        "FROM pipeline_runs ORDER BY id"
    )
    runs = [
        LegacyPipelineRun(
            id=str(row[0]),
            source_id=str(row[1]),
            source_kind=str(row[2]),
            parameters=(row[3] if isinstance(row[3], dict) else json.loads(row[3])),
            status=str(row[4]),
            started_at=row[5],
            finished_at=row[6],
        )
        for row in cur.fetchall()
    ]
    plan = build_legacy_migration_plan(runs, legacy_clean_map)

    snapshots: dict[str, tuple[int, int, int]] = {}
    for table in (*_CLEAN_STAGE_TABLES, *_DETECT_STAGE_TABLES):
        cur.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT id), "
            "COALESCE(SUM(hashtextextended("
            "(to_jsonb(artifact) - 'run_id')::text, 0)), 0) "
            f"FROM {table} AS artifact"
        )
        row = cur.fetchone()
        snapshots[table] = (int(row[0]), int(row[1]), int(row[2]))

    cur.execute(registry_ddl["clean_runs"])
    cur.execute(registry_ddl["detect_runs"])

    for run in plan.clean_runs:
        cur.execute(
            "INSERT INTO clean_runs "
            "(id, source_id, source_kind, parameters, status, started_at, finished_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (
                run.id, run.source_id, run.source_kind,
                json.dumps(run.parameters, ensure_ascii=False), run.status,
                run.started_at, run.finished_at,
            ),
        )
    for run in plan.detect_runs:
        cur.execute(
            "INSERT INTO detect_runs "
            "(id, clean_id, parameters, status, started_at, finished_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (
                run.id, run.clean_id,
                json.dumps(run.parameters, ensure_ascii=False), run.status,
                run.started_at, run.finished_at,
            ),
        )

    for table in (*_CLEAN_STAGE_TABLES, *_DETECT_STAGE_TABLES):
        cur.execute(
            "DO $$ DECLARE item record; BEGIN "
            "FOR item IN SELECT conname FROM pg_constraint "
            f"WHERE conrelid = '{table}'::regclass AND contype = 'f' LOOP "
            f"EXECUTE format('ALTER TABLE {table} DROP CONSTRAINT %I', item.conname); "
            "END LOOP; END $$"
        )

    for table in _CLEAN_STAGE_TABLES:
        cur.execute(f"ALTER TABLE {table} RENAME COLUMN run_id TO clean_id")
        cur.execute(f"ALTER TABLE {table} ALTER COLUMN clean_id SET NOT NULL")
        cur.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_clean_id_fkey "
            "FOREIGN KEY (clean_id) REFERENCES clean_runs(id)"
        )
    for table in _DETECT_STAGE_TABLES:
        cur.execute(f"ALTER TABLE {table} RENAME COLUMN run_id TO detect_id")
        cur.execute(f"ALTER TABLE {table} ALTER COLUMN detect_id SET NOT NULL")
        cur.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_detect_id_fkey "
            "FOREIGN KEY (detect_id) REFERENCES detect_runs(id)"
        )

    for table in (
        "turn_events", "candidate_intersections",
        "candidate_entry_exit_points", "intersections",
    ):
        cur.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_id_detect_id_key "
            "UNIQUE (id, detect_id)"
        )

    composite_constraints = (
        (
            "candidate_intersection_events", "candidate_intersection",
            "candidate_intersection_id", "candidate_intersections",
        ),
        (
            "candidate_intersection_events", "turn_event",
            "turn_event_id", "turn_events",
        ),
        (
            "candidate_entry_exit_points", "candidate_intersection",
            "candidate_intersection_id", "candidate_intersections",
        ),
        (
            "intersections", "candidate_intersection",
            "candidate_intersection_id", "candidate_intersections",
        ),
        (
            "entry_exit_points", "intersection",
            "intersection_id", "intersections",
        ),
        (
            "entry_exit_points", "entry_crossing",
            "entry_crossing_id", "candidate_entry_exit_points",
        ),
        (
            "entry_exit_points", "exit_crossing",
            "exit_crossing_id", "candidate_entry_exit_points",
        ),
    )
    for child, label, child_id, parent in composite_constraints:
        cur.execute(
            f"ALTER TABLE {child} ADD CONSTRAINT {child}_{label}_detect_fkey "
            f"FOREIGN KEY ({child_id}, detect_id) "
            f"REFERENCES {parent}(id, detect_id)"
        )

    for index_name in (
        "idx_ktp_vehicle_sub", "idx_ip_vehicle_ts", "idx_tc_vehicle_ts",
        "idx_tc_segment_pair", "idx_te_vehicle_ts", "idx_ci_cluster_id",
        "idx_cie_ci", "idx_ceep_cluster", "idx_int_cluster_id",
        "idx_eep_cluster_vehicle",
    ):
        cur.execute(f"DROP INDEX IF EXISTS {index_name}")
    for _, index_sql in index_statements:
        cur.execute(index_sql)

    _validate_legacy_split(cur, plan, snapshots)
    cur.execute("DROP TABLE pipeline_runs")
    return True


def _validate_legacy_split(
    cur: Any,
    plan: LegacyMigrationPlan,
    snapshots: dict[str, tuple[int, int, int]],
) -> None:
    cur.execute("SELECT COUNT(*) FROM clean_runs")
    if int(cur.fetchone()[0]) != len(plan.clean_runs):
        raise RuntimeError("legacy migration changed the Clean Run row count")
    cur.execute("SELECT COUNT(*) FROM detect_runs")
    if int(cur.fetchone()[0]) != len(plan.detect_runs):
        raise RuntimeError("legacy migration changed the Detect Run row count")

    cur.execute(
        "SELECT COUNT(*) FROM clean_runs AS c JOIN pipeline_runs AS p ON p.id = c.id "
        "WHERE c.source_id IS DISTINCT FROM p.source_id "
        "OR c.source_kind IS DISTINCT FROM p.source_kind "
        "OR c.parameters IS DISTINCT FROM p.parameters "
        "OR c.status IS DISTINCT FROM p.status "
        "OR c.started_at IS DISTINCT FROM p.started_at "
        "OR c.finished_at IS DISTINCT FROM p.finished_at"
    )
    if int(cur.fetchone()[0]) != 0:
        raise RuntimeError("legacy Clean Run payload changed during migration")
    cur.execute(
        "SELECT COUNT(*) FROM detect_runs AS d JOIN pipeline_runs AS p ON p.id = d.id "
        "WHERE d.parameters IS DISTINCT FROM p.parameters "
        "OR d.status IS DISTINCT FROM p.status "
        "OR d.started_at IS DISTINCT FROM p.started_at "
        "OR d.finished_at IS DISTINCT FROM p.finished_at"
    )
    if int(cur.fetchone()[0]) != 0:
        raise RuntimeError("legacy Detect Run payload changed during migration")

    for table, (expected_rows, expected_keys, expected_payload_hash) in snapshots.items():
        stage_column = "clean_id" if table in _CLEAN_STAGE_TABLES else "detect_id"
        registry = "clean_runs" if table in _CLEAN_STAGE_TABLES else "detect_runs"
        cur.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT id), "
            f"COUNT(*) FILTER (WHERE {stage_column} IS NULL), "
            "COALESCE(SUM(hashtextextended("
            f"(to_jsonb(artifact) - '{stage_column}')::text, 0)), 0) "
            f"FROM {table} AS artifact"
        )
        row_count, key_count, null_count, payload_hash = (
            int(value) for value in cur.fetchone()
        )
        if (
            (row_count, key_count, payload_hash)
            != (expected_rows, expected_keys, expected_payload_hash)
            or null_count
        ):
            raise RuntimeError(f"legacy migration did not preserve {table}")
        cur.execute(
            f"SELECT COUNT(*) FROM {table} AS artifact "
            f"LEFT JOIN {registry} AS registry ON registry.id = artifact.{stage_column} "
            "WHERE registry.id IS NULL"
        )
        if int(cur.fetchone()[0]) != 0:
            raise RuntimeError(f"legacy migration left orphan rows in {table}")
