"""Read-only v3 benchmark for the PostgreSQL source → Cleaner hot loop.

The v3 Pipeline no longer copies raw rows into an internal working table and
does not mutate ``trajectory_points.is_active``. This benchmark therefore
measures only the live work that remains: ordered source fetch/parsing and the
three Cleaner computations.

Usage::

    TRACK_BENCH_PROCESSING_SRID=32650 \
      python -m track.v2.manual_thin_benchmark.manual_bench_read_clean
"""

from __future__ import annotations

import os
import time

from track.v2 import get_database
from track.v2.cleaner import _exclude_stationary, _inject_orthogonal, _split_by_gaps
from track.v2.db.schema import require_schema_v3
from track.v2.db.source_table import source_order_query, validate_source_table
from track.v2.manual_thin_benchmark._thin_bench_helpers import (
    TEST_DB_NAME,
    connect_test_db,
)
from track.v2.models import CleanerConfig, PostgresSource, ProjectionConfig
from track.v2.projection import create_spatial_context
from track.v2.sources import StreamVehicleSource


SOURCE_TABLE = os.environ.get("TRACK_BENCH_SOURCE_TABLE", "trajectory_points")
MAX_VEHICLES = int(os.environ.get("TRACK_BENCH_MAX_VEHICLES", "100000"))
VERBOSE = os.environ.get("TRACK_BENCH_VERBOSE", "1") == "1"
PROCESSING_SRID = (
    int(value)
    if (value := os.environ.get("TRACK_BENCH_PROCESSING_SRID"))
    else None
)


def _percentage(value: float, total: float) -> float:
    return value / total * 100.0 if total else 0.0


def main() -> None:
    if PROCESSING_SRID is None:
        raise SystemExit(
            "TRACK_BENCH_PROCESSING_SRID is required; choose the projected "
            "metre CRS for the complete benchmark region"
        )
    spatial_context = create_spatial_context(
        ProjectionConfig(processing_srid=PROCESSING_SRID)
    )
    config = CleanerConfig()

    print("=" * 72)
    print("  manual_bench_read_clean — schema-v3 Read→Clean benchmark")
    print(f"  DB: {TEST_DB_NAME}")
    print(f"  Source table: {SOURCE_TABLE}")
    print(f"  Max vehicles: {MAX_VEHICLES if MAX_VEHICLES > 0 else 'ALL'}")
    print(f"  Verbose: {VERBOSE}")
    print("=" * 72)

    conn = connect_test_db()
    handle = None
    stream = None
    try:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout TO '10min'")
        require_schema_v3(conn)
        contract = validate_source_table(conn, SOURCE_TABLE)
        source = PostgresSource(
            config=None,  # type: ignore[arg-type] -- factory owns connections
            query=source_order_query(contract),
            source_id=SOURCE_TABLE,
            primary_key_field="id",
            connection_factory=lambda _config: connect_test_db(),
        )
        handle = get_database(source)
        stream = StreamVehicleSource(handle)

        count_started = time.perf_counter()
        estimated_rows = handle.count_rows()
        print(
            f"  Estimated rows: {estimated_rows:,} "
            f"({time.perf_counter() - count_started:.3f}s)"
        )

        split_s = 0.0
        exclude_s = 0.0
        inject_s = 0.0
        total_points = 0
        total_excluded = 0
        total_injected = 0
        total_kept = 0
        vehicle_count = 0
        loop_started = time.perf_counter()

        for vehicle_points in stream.iter_vehicle_tracks():
            vehicle_count += 1
            total_points += len(vehicle_points)

            started = time.perf_counter()
            subtracks = _split_by_gaps(vehicle_points, config.max_gap_s)
            split_s += time.perf_counter() - started

            started = time.perf_counter()
            kept_subtracks: list[list] = []
            excluded_count = 0
            for subtrack in subtracks:
                kept, excluded = _exclude_stationary(subtrack, config)
                kept_subtracks.append(kept)
                excluded_count += len(excluded)
                total_kept += len(kept)
            exclude_s += time.perf_counter() - started

            started = time.perf_counter()
            injected_count = 0
            for subtrack in kept_subtracks:
                _resolved, count, _derived = _inject_orthogonal(
                    subtrack,
                    config,
                    spatial_context=spatial_context,
                )
                injected_count += count
            inject_s += time.perf_counter() - started

            total_excluded += excluded_count
            total_injected += injected_count
            if VERBOSE and (vehicle_count <= 5 or vehicle_count % 100 == 0):
                elapsed = time.perf_counter() - loop_started
                rate = total_points / elapsed if elapsed else 0.0
                print(
                    f"  [vehicle {vehicle_count:5d}] points={len(vehicle_points):4d} "
                    f"excluded={excluded_count:3d} injected={injected_count:2d} "
                    f"total={total_points:>10,} rate={rate:,.0f} points/s"
                )
            if MAX_VEHICLES > 0 and vehicle_count >= MAX_VEHICLES:
                break

        loop_s = time.perf_counter() - loop_started
        cleaner_s = split_s + exclude_s + inject_s
        read_parse_s = max(0.0, loop_s - cleaner_s)

        print("\n" + "=" * 72)
        print("SUMMARY / 汇总")
        print("=" * 72)
        print(f"  Vehicles:              {vehicle_count:,}")
        print(f"  Source points:          {total_points:,}")
        print(f"  Kept source points:     {total_kept:,}")
        print(f"  Excluded source points: {total_excluded:,}")
        print(f"  Injected points:        {total_injected:,}")
        print(f"  Total loop:             {loop_s:8.3f}s")
        print(
            f"  Read + parse residual:  {read_parse_s:8.3f}s "
            f"({_percentage(read_parse_s, loop_s):5.1f}%)"
        )
        print(
            f"  Split by gaps:          {split_s:8.3f}s "
            f"({_percentage(split_s, loop_s):5.1f}%)"
        )
        print(
            f"  Exclude stationary:     {exclude_s:8.3f}s "
            f"({_percentage(exclude_s, loop_s):5.1f}%)"
        )
        print(
            f"  Inject orthogonal:      {inject_s:8.3f}s "
            f"({_percentage(inject_s, loop_s):5.1f}%)"
        )
        if total_points:
            print(f"  Throughput:             {total_points / loop_s:,.0f} points/s")
            print(f"  Wall per point:         {loop_s / total_points * 1e6:.2f} us")
        conn.rollback()
    finally:
        if stream is not None:
            stream.close()
        elif handle is not None:
            handle.close()
        conn.close()


if __name__ == "__main__":
    main()
