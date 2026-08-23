"""Database-backed orchestration for the track.v2 pipeline.

The pipeline entry point owns the existing Parser/Cleaner, Intersect2, Clip,
Fitting, and Network ordering and transaction boundaries.  The CLI delegates to
this module and remains responsible only for command-line concerns.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path

import psycopg

from .cleaner import clean_vehicle
from .clip import extract_clips_for_detect
from .config import PostgresConfig, connect as pg_connect
from .config.pipeline_config import load_pipeline_config
from .db_writer import (
    create_clean_run,
    create_detect_run,
    create_fit_run,
    fetch_clean_run,
    finish_clean_run,
    finish_detect_run,
    finish_fit_run,
    insert_candidate_entry_exit_points,
    insert_candidate_intersection_events,
    insert_candidate_intersections,
    insert_centerline_bundle,
    insert_entry_exit_points,
    insert_injected_points,
    insert_intersections,
    insert_kept_trajectory_points,
    insert_tracks_cross,
    insert_trajectory_points,
    insert_turn_events,
    mark_excluded_points,
)
from .fitting import fit_centerline_variants
from .intersect2 import detect_intersections
from .logger import LiveStatus
from .models import (
    ClipConfig,
    ClipResult,
    CsvSource,
    FitResult,
    FittingConfig,
    GeneratedReason,
    NetworkResult,
    PostgresSource,
    RefinementSubtrack,
)
from .network import build_network
from .parser0 import get_database
from .sources import StreamVehicleSource


@dataclass(frozen=True, slots=True)
class PipelineInput:
    """Inputs accepted by the database-backed pipeline runner."""

    db_config: PostgresConfig
    csv_path: str | None = None
    pg_source_table: str | None = None
    clean_id: str | None = None
    table: str = "trajectory_points"
    config_path: str | Path | None = None


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Persisted row counts and stage summary produced by one pipeline run."""

    stats: dict[str, int | str | list[int]]


def run_pipeline(pipeline_input: PipelineInput) -> PipelineResult:
    """Run the full pipeline with its existing write and commit ordering."""
    db_config = pipeline_input.db_config
    csv_path = pipeline_input.csv_path
    pg_source_table = pipeline_input.pg_source_table
    clean_id = pipeline_input.clean_id
    table = pipeline_input.table
    config_path = pipeline_input.config_path

    pc = load_pipeline_config(config_path)

    if sum(value is not None for value in (csv_path, pg_source_table, clean_id)) != 1:
        raise ValueError("exactly one of csv_path, pg_source_table, or clean_id is required")

    stats: dict[str, int | str | list[int]] = {}
    t_start = time.perf_counter()
    reuse_clean = clean_id is not None
    is_pg_source = pg_source_table is not None

    if csv_path is not None:
        source = CsvSource(path=csv_path)
        source_label = csv_path
        source_id = str(Path(csv_path).resolve())
        source_kind = "csv"
    elif pg_source_table is not None:
        source = PostgresSource(
            config=db_config,
            query=f"SELECT * FROM {pg_source_table} ORDER BY vehicle_id, timestamp",
            source_id=pg_source_table,
            primary_key_field="id",
        )
        source_label = f"postgresql://{db_config.database}/{pg_source_table}"
        source_id = str(pg_source_table)
        source_kind = "postgres_table"

    with pg_connect(db_config) as conn:
        if reuse_clean:
            clean_record = fetch_clean_run(conn, str(clean_id))
            if clean_record.status != "completed":
                raise ValueError(f"Clean Run {clean_id} is not completed")
            table = str(clean_record.parameters["target_table"])
            source_id = clean_record.source_id
            source_kind = clean_record.source_kind
            is_pg_source = source_kind == "postgres_table"
        else:
            clean_id = create_clean_run(
                conn,
                source_id=source_id,
                source_kind=source_kind,
                parameters={
                    "target_table": table,
                    "cleaner_max_gap_s": pc.cleaner.max_gap_s,
                },
            )
            conn.commit()
        stats["clean_id"] = str(clean_id)

        if reuse_clean:
            total_raw = 0
            total_injected = 0
            total_excluded = 0
            total_kept = 0
            tp_id_map: dict[tuple[str, str], int] = {}
        else:
            assert clean_id is not None
            print("── Parser 0 + Cleaner 1: 流式读取&清洗 / Streaming read & clean ──")
            print(f"  数据源 / Source: {source_label}")
            handle = get_database(source, runtime=pc.runtime)

            t_count = time.perf_counter()
            total_rows = handle.count_rows()
            print(f"  总行数 / Total rows: {total_rows:,} ({time.perf_counter() - t_count:.1f}s)")
            total_raw = 0
            total_injected = 0
            tp_id_map = {}
            total_excluded = 0
            total_kept = 0
            _pending_excluded: list = []
            _pending_subtracks: list[RefinementSubtrack] = []
            _pending_tp_keys: list[tuple[str, str]] = []
            subtrack_index = 1

            stream = StreamVehicleSource(handle)
            live_clean = LiveStatus("Parser 0 + Cleaner 1: 流式清洗 / Streaming Clean")
            live_clean.start()

            t_read = time.perf_counter()
            vehicle_count = 0
            last_update_t = t_read
            for vehicle_points in stream.iter_vehicle_tracks():
                total_raw += len(vehicle_points)
                vehicle_count += 1
                if not is_pg_source:
                    batch_map = insert_trajectory_points(
                        conn, vehicle_points, table=table,
                        batch_size=pc.runtime.db_insert_batch,
                    )
                    tp_id_map.update(batch_map)
                    _pending_tp_keys.extend(batch_map.keys())
                vresult = clean_vehicle(vehicle_points, config=pc.cleaner)
                total_injected += vresult.derived_counts.get(GeneratedReason.HEADING_ORTHOGONAL, 0)
                _pending_excluded.extend(vresult.excluded)
                total_excluded += len(vresult.excluded)
                _pending_subtracks.extend(vresult.subtracks)
                if len(_pending_excluded) >= pc.runtime.excluded_flush_threshold or \
                (vehicle_count % pc.runtime.mark_flush_every_n) == 0:
                    subtrack_index, batch_kept = _flush_clean_batch(
                        conn,
                        clean_id=clean_id,
                        table=table,
                        tp_id_map=tp_id_map,
                        pending_excluded=_pending_excluded,
                        pending_subtracks=_pending_subtracks,
                        pending_tp_keys=_pending_tp_keys,
                        subtrack_index=subtrack_index,
                        batch_size=pc.runtime.db_insert_batch,
                    )
                    total_kept += batch_kept

                now = time.perf_counter()
                if vehicle_count == 1 or vehicle_count % pc.runtime.progress_interval == 0 or (now - last_update_t) >= pc.runtime.progress_time_threshold_s:
                    elapsed = now - t_read
                    rate = total_raw / elapsed if elapsed > 0 else 0
                    pct = total_raw / total_rows if total_rows > 0 else 0
                    live_clean.update(
                        progress=pct,
                        detail=f"{total_raw:,}/{total_rows:,} 点 / {vehicle_count:,} 车辆",
                        elapsed=elapsed,
                        throughput=f"{rate:.0f} 点/s",
                    )
                    last_update_t = now

            subtrack_index, batch_kept = _flush_clean_batch(
                conn,
                clean_id=clean_id,
                table=table,
                tp_id_map=tp_id_map,
                pending_excluded=_pending_excluded,
                pending_subtracks=_pending_subtracks,
                pending_tp_keys=_pending_tp_keys,
                subtrack_index=subtrack_index,
                batch_size=pc.runtime.db_insert_batch,
            )
            total_kept += batch_kept
            elapsed_read = time.perf_counter() - t_read
            live_clean.update(
                progress=1.0,
                detail=f"{total_raw:,}/{total_rows:,} 点 / {vehicle_count:,} 车辆",
                elapsed=elapsed_read,
                throughput=f"{total_raw / elapsed_read:.0f} 点/s" if elapsed_read > 0 else "",
            )
            live_clean.stop()
            stream.close()
            handle.close()
            print(f"  读取&清洗 / Read&Clean: {total_raw:,} 点/points, "
                  f"{vehicle_count:,} vehicles ({elapsed_read:.1f}s)")

            stats["trajectory_points"] = total_raw

        print(f"  排除 / Excluded: {total_excluded:,}")
        print(f"  注入 / Injected: {total_injected:,}")
        if not reuse_clean:
            stats["kept_trajectory_points"] = total_kept
            stats["injected_points"] = total_injected
            print(f"  kept_trajectory_points: {total_kept:,} rows")
            print(f"  injected_points: {total_injected:,} rows")

        if not reuse_clean:
            finish_clean_run(conn, clean_id, status="completed")
            conn.commit()

        detect_id = create_detect_run(
            conn,
            clean_id=clean_id,
            parameters={"target_table": table},
        )
        stats["detect_id"] = detect_id
        conn.commit()

        print("\n── Intersect2: 交叉口检测 / Intersection Detection (DB 滑窗模式) ──")
        intersect_source_id = source_id
        live_intersect = LiveStatus("Intersect2: Cross-track Detection")
        live_intersect.start()
        icfg = replace(
            pc.detect,
            query_active_from_db=True,
            db_table=table,
            kept_table="kept_trajectory_points",
            source_id=intersect_source_id,
            clean_id=clean_id,
            live_status=live_intersect,
        )
        iresult = detect_intersections(conn=conn, config=icfg)
        live_intersect.stop()

        print(f"  转向事件 / Turn events: {iresult.report.turn_event_count:,}")
        print(f"  热点事件 / Hotspot events: {iresult.report.hotspot_event_count:,}")
        print(f"  候选交叉口 / Clusters: {iresult.report.cluster_count:,}")
        print(f"  完整交叉口 / Complete intersections: {iresult.report.complete_intersection_count:,}")

        t0 = time.perf_counter()
        tracks_cross_count = insert_tracks_cross(
            conn, iresult.cross_track_candidates, detect_id=detect_id,
            batch_size=pc.runtime.db_insert_batch,
        )
        stats["tracks_cross"] = tracks_cross_count
        print(f"  tracks_cross: {tracks_cross_count:,} rows ({time.perf_counter() - t0:.1f}s)")

        t0 = time.perf_counter()
        te_id_map = insert_turn_events(
            conn, iresult.turn_events, detect_id=detect_id,
            batch_size=pc.runtime.db_insert_batch,
        )
        stats["turn_events"] = len(iresult.turn_events)
        print(f"  turn_events: {len(iresult.turn_events):,} rows ({time.perf_counter() - t0:.1f}s)")

        t0 = time.perf_counter()
        ci_id_map = insert_candidate_intersections(conn, iresult.clusters, detect_id=detect_id)
        stats["candidate_intersections"] = len(iresult.clusters)
        print(f"  candidate_intersections: {len(iresult.clusters):,} rows ({time.perf_counter() - t0:.1f}s)")

        t0 = time.perf_counter()
        insert_candidate_intersection_events(
            conn, iresult.clusters, ci_id_map, te_id_map, detect_id=detect_id,
            batch_size=pc.runtime.db_insert_batch,
        )
        stats["candidate_intersection_events"] = sum(len(c.events) for c in iresult.clusters)
        print(f"  candidate_intersection_events: {stats['candidate_intersection_events']:,} rows ({time.perf_counter() - t0:.1f}s)")

        t0 = time.perf_counter()
        ceep_id_map = insert_candidate_entry_exit_points(
            conn, iresult.clusters, ci_id_map, detect_id=detect_id,
            batch_size=pc.runtime.db_insert_batch,
        )
        ceep_count = sum(len(c.crossing_points) for c in iresult.clusters)
        stats["candidate_entry_exit_points"] = ceep_count
        print(f"  candidate_entry_exit_points: {ceep_count:,} rows ({time.perf_counter() - t0:.1f}s)")

        clusters_by_id = {c.cluster_id: c for c in iresult.clusters}
        t0 = time.perf_counter()
        int_id_map = insert_intersections(
            conn, iresult.topologies, ci_id_map, clusters_by_id, detect_id=detect_id,
        )
        stats["intersections"] = len(int_id_map)
        print(f"  intersections: {len(int_id_map):,} rows ({time.perf_counter() - t0:.1f}s)")

        t0 = time.perf_counter()
        insert_entry_exit_points(
            conn, iresult.observations, int_id_map, ceep_id_map, detect_id=detect_id,
            batch_size=pc.runtime.db_insert_batch,
        )
        entry_exit_count = sum(
            1 for observation in iresult.observations
            if observation.cluster_id in int_id_map
        )
        stats["entry_exit_points"] = entry_exit_count
        print(f"  entry_exit_points: {entry_exit_count:,} rows ({time.perf_counter() - t0:.1f}s)")

        finish_detect_run(conn, detect_id, status="completed")
        conn.commit()

        print("\n── Clip: 路段切分 / Track clipping ──")
        clip_config = ClipConfig()
        clip_result = extract_clips_for_detect(
            conn, detect_id, config=clip_config,
        )
        _add_clip_stats(stats, clip_result)
        _print_clip_report(clip_result)

        print("\n── Fitting: 中心线拟合 / Centerline fitting ──")
        fitting_config = FittingConfig()
        fit_id = create_fit_run(
            conn,
            detect_id=detect_id,
            clip_config=clip_config,
            fitting_config=fitting_config,
        )
        stats["fit_id"] = fit_id
        conn.commit()
        try:
            fit_bundle = fit_centerline_variants(clip_result, config=fitting_config)
            centerline_ids = insert_centerline_bundle(
                conn, fit_bundle, clip_result, fit_id=fit_id, detect_id=detect_id,
            )
            report = {
                "combined": {
                    "report": fit_bundle.combined.report.as_dict(),
                    "issues": [issue.as_dict() for issue in fit_bundle.combined.issues],
                },
                "a_to_b": {
                    "report": fit_bundle.a_to_b.report.as_dict(),
                    "issues": [issue.as_dict() for issue in fit_bundle.a_to_b.issues],
                },
                "b_to_a": {
                    "report": fit_bundle.b_to_a.report.as_dict(),
                    "issues": [issue.as_dict() for issue in fit_bundle.b_to_a.issues],
                },
            }
            finish_fit_run(conn, fit_id, status="completed", report=report)
            conn.commit()
        except (psycopg.Error, RuntimeError, ValueError):
            conn.rollback()
            finish_fit_run(conn, fit_id, status="failed")
            conn.commit()
            raise
        stats["centerlines"] = len(centerline_ids)
        stats["centerline_ids"] = list(centerline_ids)
        _add_fit_stats(stats, fit_bundle.combined)
        fit_bundle.combined.report.format_print(title="  Fitting 报告 / Fitting report")

        print("\n── Network: 路网组装 / Network assembly ──")
        network_result = build_network(fit_bundle.combined, iresult)
        _add_network_stats(stats, network_result)
        network_result.report.format_print(title="  Network 报告 / Network report")

        print("\n  提交 / Committed.")

    elapsed = time.perf_counter() - t_start
    stats["_elapsed_s"] = int(elapsed)
    print(f"\n── 完成 / Done ({elapsed:.1f}s) ──")
    return PipelineResult(stats=stats)


def _flush_clean_batch(
    conn,
    *,
    clean_id: str,
    table: str,
    tp_id_map: dict[tuple[str, str], int],
    pending_excluded: list,
    pending_subtracks: list[RefinementSubtrack],
    pending_tp_keys: list[tuple[str, str]],
    subtrack_index: int,
    batch_size: int,
) -> tuple[int, int]:
    """Flush one batch of Cleaner 1 outputs and trim ``tp_id_map``."""
    if pending_excluded:
        mark_map = tp_id_map if table == "trajectory_points" else {}
        pending_excluded.clear()
    kept_count = 0
    if pending_subtracks:
        assigned_subtracks = [
            replace(subtrack, subtrack_index=subtrack_index + offset)
            for offset, subtrack in enumerate(pending_subtracks)
        ]
        insert_kept_trajectory_points(
            conn, assigned_subtracks, tp_id_map, clean_id=clean_id,
        )
        insert_injected_points(
            conn, assigned_subtracks, clean_id=clean_id, batch_size=batch_size,
        )
        kept_count = sum(
            1
            for subtrack in assigned_subtracks
            for point in subtrack.points
            if not point.point_ref.generated_reason
        )
        subtrack_index += len(assigned_subtracks)
        pending_subtracks.clear()
    for key in pending_tp_keys:
        tp_id_map.pop(key, None)
    pending_tp_keys.clear()
    return subtrack_index, kept_count


def _add_clip_stats(stats: dict[str, int | str | list[int]], result: ClipResult) -> None:
    stats.update(
        {
            f"clip_{name}": value
            for name, value in result.report.as_dict().items()
            if isinstance(value, int)
        }
    )


def _print_clip_report(result: ClipResult) -> None:
    result.report.format_print(title="  Clip 报告 / Clip report")


def _add_fit_stats(stats: dict[str, int | str | list[int]], result: FitResult) -> None:
    stats.update(
        {
            f"fitting_{name}": value
            for name, value in result.report.as_dict().items()
            if isinstance(value, int)
        }
    )


def _add_network_stats(stats: dict[str, int | str | list[int]], result: NetworkResult) -> None:
    stats.update(
        {
            f"network_{name}": value
            for name, value in result.report.as_dict().items()
            if isinstance(value, int)
        }
    )
