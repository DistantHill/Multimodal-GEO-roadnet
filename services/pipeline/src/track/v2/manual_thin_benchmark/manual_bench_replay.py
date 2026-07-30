"""Benchmark persisted replay's live PostgreSQL and CSV hot paths.

Usage::

    uv run python track/v2/manual_thin_benchmark/manual_bench_replay.py --rows 10000

Prerequisite: initialise the isolated ``track_test`` database with
``manual_setup_test_db.py``. The benchmark inserts a small fixture inside one
transaction and always rolls it back.

ZH: measure the live PostgreSQL parent chain (candidate -> member -> raw
point), first full CSV scan, and cached CSV lookup. ``--rows`` is the replay
iteration count; ``--csv-rows`` controls the scanned CSV size.
EN: Measures the live PostgreSQL parent chain (candidate -> member -> raw
point), first full CSV scan, and cached CSV lookup. ``--rows`` is the replay
iteration count; ``--csv-rows`` controls the scanned CSV size.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


# Permit direct execution without an editable installation, like the other
# thin benchmark scripts in this directory.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from _thin_bench_helpers import TEST_DB_NAME, connect_test_db
from track.v2.replay import (
    PostgresReplayStore,
    PostgresSourceRowResolver,
    ReplayService,
    ReplayTarget,
)
from track.v2.sources import source_row_hash


def _write_csv(path: Path, rows: int) -> dict[str, str]:
    raw_row = {"id": "1", "vehicle_id": "bench", "lon": "116.0"}
    last_row = raw_row
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(raw_row))
        writer.writeheader()
        for index in range(rows):
            last_row = {**raw_row, "id": str(index + 1)}
            writer.writerow(last_row)
    return last_row


def _seed_candidate_chain(
    conn: Any,
    source_id: str,
    source_row_key: str,
    raw_row: dict[str, str],
) -> tuple[str, int]:
    """Insert one candidate/member/raw chain; caller owns the transaction."""
    run_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pipeline_runs (id, source_id, source_kind, parameters, status) "
            "VALUES (%s, %s, 'csv', '{}', 'running')",
            (run_id, source_id),
        )
        cur.execute(
            "INSERT INTO trajectory_points "
            "(source_id, source_row_key, source_row_hash, vehicle_id, timestamp, lon, lat) "
            "VALUES (%s, %s, %s, 'bench', 0, 116.0, 39.0)",
            (source_id, source_row_key, source_row_hash(raw_row)),
        )
        cur.execute(
            "INSERT INTO candidate_intersections "
            "(run_id, cluster_id, centroid_x, centroid_y, centroid_lon, centroid_lat, radius_m, origin_lon, origin_lat) "
            "VALUES (%s, 1, 0, 0, 116.0, 39.0, 1, 116.0, 39.0) RETURNING id",
            (run_id,),
        )
        candidate_id = int(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO candidate_intersection_events "
            "(run_id, candidate_intersection_id, event_kind, from_source_id, from_source_row_key, to_source_id, to_source_row_key) "
            "VALUES (%s, %s, 'benchmark', %s, %s, %s, %s)",
            (run_id, candidate_id, source_id, source_row_key, source_id, source_row_key),
        )
    return run_id, candidate_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark v2 persisted replay hot paths.")
    parser.add_argument("--rows", type=int, default=10_000, help="Replay iterations.")
    parser.add_argument("--csv-rows", type=int, default=10_000, help="Rows in the scanned CSV.")
    args = parser.parse_args()
    verbose = os.environ.get("VERBOSE") == "1"
    if args.rows < 1 or args.csv_rows < 1:
        parser.error("--rows and --csv-rows must be positive")

    with tempfile.TemporaryDirectory(prefix="track-v2-replay-bench-") as directory:
        csv_path = Path(directory) / "source.csv"
        raw_row = _write_csv(csv_path, args.csv_rows)
        source_row_key = str(args.csv_rows + 1)
        conn = connect_test_db()
        try:
            run_id, candidate_id = _seed_candidate_chain(
                conn, str(csv_path), source_row_key, raw_row,
            )
            store = PostgresReplayStore(conn)
            resolver = PostgresSourceRowResolver(conn)

            started = time.perf_counter()
            resolver(str(csv_path), source_row_key)
            csv_first_scan_seconds = time.perf_counter() - started
            started = time.perf_counter()
            resolver(str(csv_path), source_row_key)
            csv_cached_lookup_seconds = time.perf_counter() - started

            replay_seconds = 0.0
            wall_start = time.perf_counter()
            for index in range(1, args.rows + 1):
                started = time.perf_counter()
                trace = ReplayService(
                    store, source_row_resolver=resolver,
                ).replay(ReplayTarget("candidate_intersection", candidate_id, run_id))
                replay_seconds += time.perf_counter() - started
                if trace.status != "ok":
                    raise RuntimeError(f"unexpected replay status: {trace.status}")
                if verbose and index % 1_000 == 0:
                    print(f"  {index:,}/{args.rows:,} replays")
            wall_seconds = time.perf_counter() - wall_start
        finally:
            conn.rollback()
            conn.close()

    print(f"database={TEST_DB_NAME}")
    print(f"replays={args.rows:,}")
    print(f"csv_rows={args.csv_rows:,}")
    print(f"wall_seconds={wall_seconds:.6f}")
    print(f"csv_first_scan_seconds={csv_first_scan_seconds:.6f}")
    print(f"csv_cached_lookup_us={csv_cached_lookup_seconds * 1_000_000:.2f}")
    print(f"postgres_parent_chain_seconds={replay_seconds:.6f} ({replay_seconds / max(wall_seconds, 1e-9):.1%})")
    print(f"postgres_parent_chain_per_replay_ms={replay_seconds / args.rows * 1_000:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
