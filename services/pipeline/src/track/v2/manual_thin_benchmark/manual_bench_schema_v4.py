"""Read-only A65 audit for the track.v2 schema-v4 cutover.

The report keeps storage, Clip query/assembly latency, persisted behavior, and
optional end-to-end Pipeline phase timings in one machine-readable document.
It deliberately has no percentage gate.  When a baseline is supplied, only a
greater-than-10x timing/bytes-per-row increase or a behavior mismatch fails.

Example::

    python -m track.v2.manual_thin_benchmark.manual_bench_schema_v4 \
      --database roadnet --detect-id UUID \
      --pipeline-stats /tmp/roadnet-v3-run.json \
      --output /tmp/roadnet-v3-benchmark.json
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..clip import extract_clips
from ..config import PostgresConfig, connect, default_postgres_config
from ..db.queries import (
    fetch_detect_row,
    fetch_intersections,
    fetch_resolved_clip_tracks,
)
from ..db.schema import require_current_schema
from ..db.source_table import verify_refine_source_fingerprint
from ..models import ClipInput


CORE_TABLES = (
    "refine_runs",
    "detect_runs",
    "fit_runs",
    "trajectory_points",
    "kept_trajectory_points",
    "excluded_points",
    "injected_points",
    "resolved_subtrack_points",
    "tracks_cross",
    "turn_events",
    "candidate_intersections",
    "candidate_entry_exit_points",
    "intersections",
    "entry_exit_points",
    "centerlines",
    "centerline_source_clips",
)

PIPELINE_TIMING_KEYS = (
    "detect_compute_s",
    "detect_persist_s",
    "resolved_sequence_s",
    "clip_assembly_s",
    "_elapsed_s",
)


def _table_metrics(conn: Any) -> dict[str, dict[str, float | int | None]]:
    metrics: dict[str, dict[str, float | int | None]] = {}
    with conn.cursor() as cur:
        for table in CORE_TABLES:
            cur.execute(
                f"SELECT COUNT(*) FROM {table}"  # noqa: S608 - fixed allowlist
            )
            row_count = int(cur.fetchone()[0])
            cur.execute(
                "SELECT pg_table_size(%s::regclass), pg_indexes_size(%s::regclass), "
                "pg_total_relation_size(%s::regclass)",
                (table, table, table),
            )
            table_bytes, index_bytes, total_bytes = map(int, cur.fetchone())
            metrics[table] = {
                "rows": row_count,
                "table_bytes": table_bytes,
                "index_bytes": index_bytes,
                "total_bytes": total_bytes,
                "table_bytes_per_row": (
                    table_bytes / row_count if row_count else None
                ),
                "total_bytes_per_row": (
                    total_bytes / row_count if row_count else None
                ),
            }
    return metrics


def _latest_completed_detect(conn: Any) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text FROM detect_runs WHERE status = 'completed' "
            "ORDER BY finished_at DESC NULLS LAST, started_at DESC LIMIT 1"
        )
        row = cur.fetchone()
    return None if row is None else str(row[0])


def _detect_counts(conn: Any, detect_id: str) -> dict[str, int]:
    columns = {
        "resolved_subtrack_points": "detect_id",
        "tracks_cross": "detect_id",
        "turn_events": "detect_id",
        "candidate_intersections": "detect_id",
        "candidate_entry_exit_points": "detect_id",
        "intersections": "detect_id",
        "entry_exit_points": "detect_id",
        "centerlines": "detect_id",
        "centerline_source_clips": "detect_id",
    }
    result: dict[str, int] = {}
    with conn.cursor() as cur:
        for table, column in columns.items():
            cur.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = %s",  # noqa: S608
                (detect_id,),
            )
            result[table] = int(cur.fetchone()[0])
    return result


def _clip_digest(result: Any) -> str:
    payload = [
        {
            "type": clip.clip_type,
            "subtrack": clip.subtrack_index,
            "start_crossing": clip.start.crossing_id,
            "end_crossing": clip.end.crossing_id,
            "start_intersection": clip.start.intersection_id,
            "end_intersection": clip.end.intersection_id,
            "resolved_points": [point.resolved_point_id for point in clip.points],
            "point_count": clip.point_count,
        }
        for clip in result.clips
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _rows_digest(rows: list[tuple[Any, ...]]) -> str:
    """Hash ordered persisted behavior without relying on display formatting."""
    digest = hashlib.sha256()
    for row in rows:
        encoded = json.dumps(
            row,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
            allow_nan=False,
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _persisted_behavior_digests(conn: Any, detect_id: str) -> dict[str, str]:
    """Digest exact crossing, pairing, and Centerline rows for one Detect."""
    queries = {
        "crossing_digest_sha256": """
            SELECT id, candidate_intersection_id, segment_start_point_id,
                   crossing_type, crossing_x, crossing_y,
                   crossing_lon, crossing_lat
              FROM candidate_entry_exit_points
             WHERE detect_id = %s
             ORDER BY id
        """,
        "entry_exit_digest_sha256": """
            SELECT id, intersection_id, entry_bearing, exit_bearing,
                   entry_crossing_id, exit_crossing_id
              FROM entry_exit_points
             WHERE detect_id = %s
             ORDER BY id
        """,
        "centerline_digest_sha256": """
            SELECT id, fit_id::text, variant,
                   from_intersection_id, to_intersection_id,
                   encode(ST_AsEWKB(geom), 'hex'), length_m,
                   forward_clip_count, reverse_clip_count
              FROM centerlines
             WHERE detect_id = %s
             ORDER BY id
        """,
    }
    result: dict[str, str] = {}
    with conn.cursor() as cur:
        for name, query in queries.items():
            cur.execute(query, (detect_id,))
            result[name] = _rows_digest(list(cur.fetchall()))
    return result


def _benchmark_clip(
    conn: Any,
    detect_id: str,
    repetitions: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    run = fetch_detect_row(conn, detect_id)
    if run.status != "completed":
        raise ValueError(f"Detect Run {detect_id} is not completed")

    fingerprint_started = time.perf_counter()
    verify_refine_source_fingerprint(conn, run.refine_id)
    fingerprint_elapsed = time.perf_counter() - fingerprint_started

    query_samples: list[float] = []
    assembly_samples: list[float] = []
    result: Any = None
    for _ in range(repetitions):
        query_started = time.perf_counter()
        intersections = tuple(fetch_intersections(conn, detect_id))
        tracks = tuple(fetch_resolved_clip_tracks(conn, detect_id))
        query_samples.append(time.perf_counter() - query_started)

        source = ClipInput(
            intersections=intersections,
            tracks=tracks,
            spatial_context=run.spatial_context,
            detect_id=detect_id,
        )
        assembly_started = time.perf_counter()
        result = extract_clips(source)
        assembly_samples.append(time.perf_counter() - assembly_started)

    assert result is not None
    timing = {
        "repetitions": repetitions,
        "source_fingerprint_verify_s": fingerprint_elapsed,
        "query_samples_s": query_samples,
        "query_median_s": statistics.median(query_samples),
        "assembly_samples_s": assembly_samples,
        "assembly_median_s": statistics.median(assembly_samples),
    }
    behavior = {
        "detect_counts": _detect_counts(conn, detect_id),
        "clip_report": result.report.as_dict(),
        "clip_digest_sha256": _clip_digest(result),
        **_persisted_behavior_digests(conn, detect_id),
    }
    return timing, behavior


def _load_pipeline_timings(path: Path | None) -> dict[str, float] | None:
    if path is None:
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    missing = [key for key in PIPELINE_TIMING_KEYS if key not in raw]
    if missing:
        raise ValueError(
            "Pipeline stats are missing A65 timing keys: " + ", ".join(missing)
        )
    return {key: float(raw[key]) for key in PIPELINE_TIMING_KEYS}


def _find_regressions(current: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    regressions: list[str] = []
    for table, metrics in current["tables"].items():
        old = baseline.get("tables", {}).get(table, {})
        for key in ("table_bytes_per_row", "total_bytes_per_row"):
            before = old.get(key)
            after = metrics.get(key)
            if before and after is not None and after > float(before) * 10.0:
                regressions.append(f"{table}.{key}: {after:g} > 10x {before:g}")

    timing_pairs = (
        ("pipeline_timings", PIPELINE_TIMING_KEYS),
        ("clip_benchmark", ("query_median_s", "assembly_median_s")),
    )
    for section, keys in timing_pairs:
        current_section = current.get(section) or {}
        baseline_section = baseline.get(section) or {}
        for key in keys:
            before = baseline_section.get(key)
            after = current_section.get(key)
            if before and after is not None and float(after) > float(before) * 10.0:
                regressions.append(f"{section}.{key}: {after:g} > 10x {before:g}")

    old_behavior = baseline.get("behavior")
    new_behavior = current.get("behavior")
    if old_behavior is not None and new_behavior != old_behavior:
        regressions.append("behavior output differs from the supplied baseline")
    return regressions


def build_parser() -> argparse.ArgumentParser:
    defaults = default_postgres_config()
    parser = argparse.ArgumentParser(description="Audit schema-v4 storage and A65 timings")
    parser.add_argument("--host", default=defaults.host)
    parser.add_argument("--port", type=int, default=defaults.port)
    parser.add_argument("--database", default=defaults.database)
    parser.add_argument("--user", default=defaults.user)
    parser.add_argument("--ask-password", action="store_true")
    parser.add_argument("--connect-timeout", type=int, default=defaults.connect_timeout)
    parser.add_argument("--sslmode", default=defaults.sslmode)
    parser.add_argument("--detect-id")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--pipeline-stats", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.repetitions < 1:
        raise SystemExit("--repetitions must be positive")
    password = (
        getpass.getpass("PostgreSQL password: ")
        if args.ask_password
        else default_postgres_config().password
    )
    config = PostgresConfig(
        host=args.host,
        port=args.port,
        database=args.database,
        user=args.user,
        password=password,
        connect_timeout=args.connect_timeout,
        sslmode=args.sslmode,
    )
    report: dict[str, Any] = {
        "schema": "track.v2-schema-v4-a65-benchmark",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "database": args.database,
    }
    with connect(config) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        require_current_schema(conn)
        report["tables"] = _table_metrics(conn)
        detect_id = args.detect_id or _latest_completed_detect(conn)
        report["detect_id"] = detect_id
        if detect_id is not None:
            clip_timing, behavior = _benchmark_clip(
                conn,
                detect_id,
                args.repetitions,
            )
            report["clip_benchmark"] = clip_timing
            report["behavior"] = behavior
        else:
            report["clip_benchmark"] = None
            report["behavior"] = None
        report["pipeline_timings"] = _load_pipeline_timings(args.pipeline_stats)
        conn.rollback()

    regressions: list[str] = []
    if args.baseline is not None:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        regressions = _find_regressions(report, baseline)
    report["order_of_magnitude_regressions"] = regressions
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    print(rendered, end="")
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 2 if regressions else 0


if __name__ == "__main__":
    raise SystemExit(main())
