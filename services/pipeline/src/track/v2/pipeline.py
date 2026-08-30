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
from .config import PostgresConfig, connect as pg_connect
from .config.pipeline_config import load_pipeline_config
from .db.pipeline_store import PipelineStore, PostgresPipelineStore
from .fitting import fit_centerline_variants
from .intersect2 import detect_intersections
from .logger import log_info
from .models import (
    ClipResult,
    CsvSource,
    FitResult,
    GeneratedReason,
    NetworkResult,
    PostgresSource,
    RefinementSubtrack,
    RuntimeConfig,
)
from .network import build_network
from .parser0 import get_database
from .pipeline_events import EventProgressAdapter, EventSink, PipelineEvent
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


def _noop_event_sink(_event: PipelineEvent) -> None:
    """Default sink for non-CLI callers."""


def run_pipeline(
    pipeline_input: PipelineInput,
    *,
    emit: EventSink | None = None,
) -> PipelineResult:
    """Run the full pipeline with its existing write and commit ordering."""
    sink: EventSink = _noop_event_sink if emit is None else emit
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
        store: PipelineStore = PostgresPipelineStore(conn)
        if reuse_clean:
            clean_record = store.get_clean_run(str(clean_id))
            if clean_record.status != "completed":
                raise ValueError(f"Clean Run {clean_id} is not completed")
            table = str(clean_record.parameters["target_table"])
            source_id = clean_record.source_id
            source_kind = clean_record.source_kind
            is_pg_source = source_kind == "postgres_table"
        else:
            clean_id = store.start_clean_run(
                source_id=source_id,
                source_kind=source_kind,
                parameters={
                    "target_table": table,
                    "cleaner_max_gap_s": pc.cleaner.max_gap_s,
                },
            )
            store.commit()
        stats["clean_id"] = str(clean_id)

        if reuse_clean:
            total_raw = 0
            total_injected = 0
            total_excluded = 0
            total_kept = 0
            tp_id_map: dict[tuple[str, str], int] = {}
        else:
            assert clean_id is not None
            log_info(f"[stage-start] stage=cleaner clean_id={clean_id}")
            clean_stage = "Parser 0 + Cleaner 1: 流式读取&清洗 / Streaming read & clean"
            live_clean = EventProgressAdapter(clean_stage, sink)
            live_clean.start()
            _emit_message(sink, f"  数据源 / Source: {source_label}")
            handle = get_database(source, runtime=pc.runtime)

            t_count = time.perf_counter()
            total_rows = handle.count_rows()
            _emit_message(
                sink,
                f"  总行数 / Total rows: {total_rows:,} ({time.perf_counter() - t_count:.1f}s)",
            )
            total_raw = 0
            total_injected = 0
            tp_id_map = {}
            total_excluded = 0
            total_kept = 0
            _pending_excluded: list = []
            _pending_subtracks: list[RefinementSubtrack] = []
            _pending_tp_keys: list[tuple[str, str]] = []
            _pending_point_count = 0
            subtrack_index = 1

            stream = StreamVehicleSource(handle)

            t_read = time.perf_counter()
            vehicle_count = 0
            last_update_t = t_read
            for vehicle_points in stream.iter_vehicle_tracks():
                total_raw += len(vehicle_points)
                vehicle_count += 1
                if not is_pg_source:
                    batch_map = store.write_raw_points(
                        vehicle_points,
                        table=table,
                        batch_size=pc.runtime.db_insert_batch,
                    )
                    tp_id_map.update(batch_map)
                    _pending_tp_keys.extend(batch_map.keys())
                vresult = clean_vehicle(vehicle_points, config=pc.cleaner)
                total_injected += vresult.derived_counts.get(GeneratedReason.HEADING_ORTHOGONAL, 0)
                _pending_excluded.extend(vresult.excluded)
                total_excluded += len(vresult.excluded)
                _pending_subtracks.extend(vresult.subtracks)
                _pending_point_count += len(vresult.excluded) + sum(
                    len(subtrack.points) for subtrack in vresult.subtracks
                )
                if _should_flush_clean_batch(
                    pending_excluded_count=len(_pending_excluded),
                    pending_point_count=_pending_point_count,
                    vehicle_count=vehicle_count,
                    runtime=pc.runtime,
                ):
                    subtrack_index, batch_kept = _flush_clean_batch(
                        store,
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
                    _pending_point_count = 0

                now = time.perf_counter()
                if vehicle_count == 1 or vehicle_count % pc.runtime.progress_interval == 0 or (now - last_update_t) >= pc.runtime.progress_time_threshold_s:
                    elapsed = now - t_read
                    rate = total_raw / elapsed if elapsed > 0 else 0
                    pct = total_raw / total_rows if total_rows > 0 else 0
                    live_clean.update(
                        progress=pct,
                        detail=f"{total_raw:,}/{total_rows:,} 点, {vehicle_count:,} 车辆",
                        elapsed=elapsed,
                        throughput=f"{rate:.0f} 点/s",
                    )
                    last_update_t = now

            subtrack_index, batch_kept = _flush_clean_batch(
                store,
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
                detail=f"{total_raw:,}/{total_rows:,} 点, {vehicle_count:,} 车辆",
                elapsed=elapsed_read,
                throughput=f"{total_raw / elapsed_read:.0f} 点/s" if elapsed_read > 0 else "",
            )
            live_clean.stop()
            stream.close()
            handle.close()
            _emit_message(
                sink,
                f"  读取&清洗 / Read&Clean: {total_raw:,} 点/points, "
                f"{vehicle_count:,} 车辆/vehicles ({elapsed_read:.1f}s)",
            )

            stats["trajectory_points"] = total_raw

        _emit_message(sink, f"  排除 / Excluded: {total_excluded:,}")
        _emit_message(sink, f"  注入 / Injected: {total_injected:,}")
        if not reuse_clean:
            stats["kept_trajectory_points"] = total_kept
            stats["injected_points"] = total_injected
            _emit_message(sink, f"  kept_trajectory_points: {total_kept:,} rows")
            _emit_message(sink, f"  injected_points: {total_injected:,} rows")

        if not reuse_clean:
            store.finish_clean(clean_id, status="completed")
            store.commit()

        detect_id = store.start_detect_run(
            clean_id=clean_id,
            parameters={"target_table": table},
        )
        stats["detect_id"] = detect_id
        store.commit()

        detect_stage = "Intersect2: 交叉口检测 / Intersection Detection (DB 滑窗模式)"
        log_info(
            f"[stage-start] stage=intersect2 clean_id={clean_id} detect_id={detect_id}"
        )
        intersect_source_id = source_id
        live_intersect = EventProgressAdapter(detect_stage, sink)
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
        iresult = detect_intersections(conn=store.connection, config=icfg)
        live_intersect.stop()

        _emit_message(sink, f"  转向事件 / Turn events: {iresult.report.turn_event_count:,}")
        _emit_message(sink, f"  热点事件 / Hotspot events: {iresult.report.hotspot_event_count:,}")
        _emit_message(sink, f"  候选交叉口 / Clusters: {iresult.report.cluster_count:,}")
        _emit_message(
            sink,
            f"  完整交叉口 / Complete intersections: {iresult.report.complete_intersection_count:,}",
        )

        persisted_detect = store.persist_detect(
            detect_id,
            iresult,
            batch_size=pc.runtime.db_insert_batch,
        )
        stats.update(
            {
                "tracks_cross": persisted_detect.tracks_cross_count,
                "turn_events": persisted_detect.turn_event_count,
                "candidate_intersections": persisted_detect.candidate_intersection_count,
                "candidate_intersection_events": persisted_detect.candidate_intersection_event_count,
                "candidate_entry_exit_points": persisted_detect.candidate_entry_exit_point_count,
                "intersections": persisted_detect.intersection_count,
                "entry_exit_points": persisted_detect.entry_exit_point_count,
            }
        )
        _emit_message(sink, f"  tracks_cross: {persisted_detect.tracks_cross_count:,} rows")
        _emit_message(sink, f"  turn_events: {persisted_detect.turn_event_count:,} rows")
        _emit_message(sink, f"  candidate_intersections: {persisted_detect.candidate_intersection_count:,} rows")
        _emit_message(
            sink,
            f"  candidate_intersection_events: {persisted_detect.candidate_intersection_event_count:,} rows",
        )
        _emit_message(
            sink,
            f"  candidate_entry_exit_points: {persisted_detect.candidate_entry_exit_point_count:,} rows",
        )
        _emit_message(sink, f"  intersections: {persisted_detect.intersection_count:,} rows")
        _emit_message(sink, f"  entry_exit_points: {persisted_detect.entry_exit_point_count:,} rows")

        store.finish_detect(detect_id, status="completed")
        store.commit()

        log_info(f"[stage-start] stage=clip clean_id={clean_id} detect_id={detect_id}")
        sink(PipelineEvent.stage_start("Clip: 路段切分 / Track clipping"))
        clip_config = pc.clip
        clip_result = store.load_clip(detect_id, config=clip_config)
        _add_clip_stats(stats, clip_result)
        _emit_report(sink, "  Clip 报告 / Clip report", clip_result.report.as_dict())
        sink(PipelineEvent.stage_finish("Clip: 路段切分 / Track clipping"))

        fitting_config = pc.fitting
        fit_id = store.start_fit_run(
            detect_id=detect_id,
            clip_config=clip_config,
            fitting_config=fitting_config,
        )
        stats["fit_id"] = fit_id
        store.commit()
        log_info(
            f"[stage-start] stage=fitting clean_id={clean_id} "
            f"detect_id={detect_id} fit_id={fit_id}"
        )
        sink(PipelineEvent.stage_start("Fitting: 中心线拟合 / Centerline fitting"))
        try:
            fit_bundle = fit_centerline_variants(clip_result, config=fitting_config)
            centerline_ids = store.persist_fit_bundle(
                fit_id,
                detect_id,
                fit_bundle,
                clip_result,
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
            store.finish_fit(fit_id, status="completed", report=report)
            store.commit()
        except (psycopg.Error, RuntimeError, ValueError):
            store.rollback()
            store.finish_fit(fit_id, status="failed")
            store.commit()
            raise
        stats["centerlines"] = len(centerline_ids)
        stats["centerline_ids"] = list(centerline_ids)
        _add_fit_stats(stats, fit_bundle.combined)
        _emit_report(
            sink,
            "  Fitting 报告 / Fitting report",
            fit_bundle.combined.report.as_dict(),
        )
        sink(PipelineEvent.stage_finish("Fitting: 中心线拟合 / Centerline fitting"))

        log_info(
            f"[stage-start] stage=network clean_id={clean_id} "
            f"detect_id={detect_id} fit_id={fit_id}"
        )
        sink(PipelineEvent.stage_start("Network: 路网组装 / Network assembly"))
        network_result = build_network(fit_bundle.combined, iresult, config=pc.network)
        _add_network_stats(stats, network_result)
        _emit_report(
            sink,
            "  Network 报告 / Network report",
            network_result.report.as_dict(),
        )
        sink(PipelineEvent.stage_finish("Network: 路网组装 / Network assembly"))

        _emit_message(sink, "\n  提交 / Committed.")

    elapsed = time.perf_counter() - t_start
    stats["_elapsed_s"] = int(elapsed)
    _emit_message(sink, f"\n── 完成 / Done ({elapsed:.1f}s) ──")
    return PipelineResult(stats=stats)


def _flush_clean_batch(
    store: PipelineStore,
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
    result = store.persist_clean_batch(
        clean_id,
        table,
        tp_id_map,
        pending_excluded,
        pending_subtracks,
        pending_tp_keys,
        subtrack_index,
        batch_size=batch_size,
    )
    return result.next_subtrack_index, result.kept_count


def _should_flush_clean_batch(
    *,
    pending_excluded_count: int,
    pending_point_count: int,
    vehicle_count: int,
    runtime: RuntimeConfig,
) -> bool:
    """Bound Cleaner pending output between complete vehicle groups."""
    return (
        pending_excluded_count >= runtime.excluded_flush_threshold
        or pending_point_count >= runtime.cleaner_flush_point_threshold
        or vehicle_count % runtime.mark_flush_every_n == 0
    )


def _emit_message(sink: EventSink, message: str) -> None:
    sink(PipelineEvent.message_event(message))


def _emit_report(sink: EventSink, title: str, report: dict[str, object]) -> None:
    _emit_message(sink, title)
    for key, value in report.items():
        rendered = (
            f"{value:,}"
            if isinstance(value, int) and not isinstance(value, bool)
            else str(value)
        )
        _emit_message(sink, f"  {key}: {rendered}")


def _add_clip_stats(stats: dict[str, int | str | list[int]], result: ClipResult) -> None:
    stats.update(
        {
            f"clip_{name}": value
            for name, value in result.report.as_dict().items()
            if isinstance(value, int)
        }
    )


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
