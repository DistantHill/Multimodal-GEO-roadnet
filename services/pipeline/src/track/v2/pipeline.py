"""Database-backed orchestration for the track.v2 pipeline.

The pipeline entry point owns the existing Parser/Refine, Detect, Clip,
Fitting, and Network ordering and transaction boundaries.  The CLI delegates to
this module and remains responsible only for command-line concerns.
"""

from __future__ import annotations

import time
from contextlib import ExitStack, suppress
from dataclasses import dataclass, replace
from uuid import uuid4

import psycopg

from .refine import refine_vehicle
from .artifact_publication import abandon_staging, create_staging, publish
from .config import PipelineConfig, connect as pg_connect
from .db.pipeline_store import PipelineStore, PostgresPipelineStore
from .db.artifact_registry import find_refine_artifact
from .db.schema import require_current_schema
from .db.source_table import (
    calculate_source_fingerprint,
    source_order_query,
    validate_source_table,
    verify_refine_source_fingerprint,  # compatibility seam; Detect owns invocation
)
from .fitting import fit_centerline_variants
from .detect import DetectRuntimeContext, detect_intersections
from .detect_parquet import export_detect_parquet
from .logger import log_info
from .heartbeat import operation_heartbeat
from .models import (
    ClipResult,
    FitResult,
    GeneratedReason,
    NetworkResult,
    PostgresSource,
    RefinementSubtrack,
    RuntimeConfig,
    SpatialContext,
)
from .network import build_network
from .parser import get_database
from .pipeline_events import EventProgressAdapter, EventSink, PipelineEvent
from .projection import CoordinateTransformer, create_spatial_context
from .parser import StreamVehicleSource
from .refine_parquet import RefineDatasetReader, export_refine_parquet


@dataclass(frozen=True, slots=True)
class PipelineInput:
    """Inputs accepted by the database-backed pipeline runner."""

    source_table: str | None = None
    refine_id: str | None = None


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Persisted row counts and stage summary produced by one pipeline run."""

    stats: dict[str, float | int | str | list[int]]


def _noop_event_sink(_event: PipelineEvent) -> None:
    """Default sink for non-CLI callers."""


def _fail_running_refine(store: PipelineStore, refine_id: str) -> None:
    """Best-effort failure convergence that never masks the original error."""
    with suppress(Exception):
        store.rollback()
    with suppress(Exception):
        if store.get_refine_run(refine_id).status == "running":
            store.finish_refine(refine_id, status="failed")
            store.commit()


def run_pipeline(
    pipeline_input: PipelineInput,
    config: PipelineConfig,
    *,
    emit: EventSink | None = None,
) -> PipelineResult:
    """Run the full pipeline with its existing write and commit ordering."""
    sink: EventSink = _noop_event_sink if emit is None else emit
    db_config = config.postgres
    source_table = pipeline_input.source_table
    refine_id = pipeline_input.refine_id
    pc = config
    config_parameters = pc.snapshot_parameters()

    if sum(value is not None for value in (source_table, refine_id)) != 1:
        raise ValueError("exactly one of source_table or refine_id is required")

    stats: dict[str, float | int | str | list[int]] = {}
    t_start = time.perf_counter()
    reuse_refine = refine_id is not None

    with ExitStack() as resources, pg_connect(db_config) as conn:
        require_current_schema(conn)
        store: PipelineStore = PostgresPipelineStore(conn)
        if reuse_refine:
            refine_record = store.get_refine_run(str(refine_id))
            if refine_record.status != "completed":
                raise ValueError(f"Refine Run {refine_id} is not completed")
            source_table = refine_record.source_table
            spatial_context = refine_record.spatial_context
        else:
            assert source_table is not None
            source_contract = validate_source_table(conn, source_table)
            source = PostgresSource(
                config=db_config,
                query=source_order_query(source_contract),
                source_id=source_table,
                primary_key_field="id",
            )
            source_label = f"postgresql://{db_config.database}/{source_table}"
            spatial_context = create_spatial_context(
                pc.projection,
                processing_srid=pc.projection.processing_srid,
            )
            refine_id = store.start_refine_run(
                source_table=source_table,
                parameters=config_parameters,
                spatial_context=spatial_context,
            )
            store.commit()
        stats["refine_id"] = str(refine_id)
        stats["source_srid"] = spatial_context.source_srid
        stats["processing_srid"] = spatial_context.processing_srid

        if reuse_refine:
            total_raw = 0
            total_injected = 0
            total_excluded = 0
            total_kept = 0
        else:
            assert refine_id is not None
            handle = None
            stream = None
            staging = None
            refine_published = False
            try:
                refine_started = _log_stage_start("refine", refine_id=refine_id)
                refine_stage = "Parser + Refine: 流式读取&精炼 / Streaming read & refine"
                live_refine = EventProgressAdapter(refine_stage, sink)
                live_refine.start()
                _emit_message(sink, f"  数据源 / Source: {source_label}")
                handle = get_database(
                    source,
                    field_config=pc.parser.fields,
                    runtime=pc.runtime,
                )
                try:
                    t_count = time.perf_counter()
                    total_rows = handle.count_rows()
                    _emit_message(
                        sink,
                        f"  总行数 / Total rows: {total_rows:,} ({time.perf_counter() - t_count:.1f}s)",
                    )
                    total_raw = 0
                    total_injected = 0
                    total_excluded = 0
                    total_kept = 0
                    _pending_excluded: list = []
                    _pending_subtracks: list[RefinementSubtrack] = []
                    _pending_point_count = 0
                    subtrack_index = 1

                    stream = StreamVehicleSource(handle)
                    run_transformer = CoordinateTransformer(spatial_context)
                    run_bounds: tuple[float, float, float, float] | None = None

                    t_read = time.perf_counter()
                    vehicle_count = 0
                    last_update_t = t_read
                    for vehicle_points in stream.iter_vehicle_tracks():
                        if vehicle_points:
                            lons = [point.lon for point in vehicle_points]
                            lats = [point.lat for point in vehicle_points]
                            vehicle_bounds = (min(lons), min(lats), max(lons), max(lats))
                            run_bounds = (
                                vehicle_bounds
                                if run_bounds is None
                                else (
                                    min(run_bounds[0], vehicle_bounds[0]),
                                    min(run_bounds[1], vehicle_bounds[1]),
                                    max(run_bounds[2], vehicle_bounds[2]),
                                    max(run_bounds[3], vehicle_bounds[3]),
                                )
                            )
                        total_raw += len(vehicle_points)
                        vehicle_count += 1
                        vresult = refine_vehicle(
                            vehicle_points,
                            config=pc.refine,
                            spatial_context=spatial_context,
                            _transformer=run_transformer,
                        )
                        total_injected += vresult.derived_counts.get(GeneratedReason.HEADING_ORTHOGONAL, 0)
                        _pending_excluded.extend(vresult.excluded)
                        total_excluded += len(vresult.excluded)
                        _pending_subtracks.extend(vresult.subtracks)
                        _pending_point_count += len(vresult.excluded) + sum(
                            len(subtrack.points) for subtrack in vresult.subtracks
                        )
                        if _should_flush_refine_batch(
                            pending_excluded_count=len(_pending_excluded),
                            pending_point_count=_pending_point_count,
                            vehicle_count=vehicle_count,
                            runtime=pc.runtime,
                        ):
                            subtrack_index, batch_kept = _flush_refine_batch(
                                store,
                                refine_id=refine_id,
                                pending_excluded=_pending_excluded,
                                pending_subtracks=_pending_subtracks,
                                subtrack_index=subtrack_index,
                                batch_size=pc.runtime.db_insert_batch,
                                spatial_context=spatial_context,
                            )
                            total_kept += batch_kept
                            _pending_point_count = 0

                        now = time.perf_counter()
                        if vehicle_count == 1 or vehicle_count % pc.runtime.progress_interval == 0 or (now - last_update_t) >= pc.runtime.progress_time_threshold_s:
                            elapsed = now - t_read
                            rate = total_raw / elapsed if elapsed > 0 else 0
                            pct = total_raw / total_rows if total_rows > 0 else 0
                            live_refine.update(
                                progress=pct,
                                detail=f"{total_raw:,}/{total_rows:,} 点, {vehicle_count:,} 车辆",
                                elapsed=elapsed,
                                throughput=f"{rate:.0f} 点/s",
                            )
                            last_update_t = now

                    if run_bounds is not None:
                        run_transformer.validate_bounds(run_bounds)

                    subtrack_index, batch_kept = _flush_refine_batch(
                        store,
                        refine_id=refine_id,
                        pending_excluded=_pending_excluded,
                        pending_subtracks=_pending_subtracks,
                        subtrack_index=subtrack_index,
                        batch_size=pc.runtime.db_insert_batch,
                        spatial_context=spatial_context,
                    )
                    total_kept += batch_kept
                    elapsed_read = time.perf_counter() - t_read
                    live_refine.update(
                        progress=1.0,
                        detail=f"{total_raw:,}/{total_rows:,} 点, {vehicle_count:,} 车辆",
                        elapsed=elapsed_read,
                        throughput=f"{total_raw / elapsed_read:.0f} 点/s" if elapsed_read > 0 else "",
                    )
                finally:
                    if stream is not None:
                        stream.close()
                    elif handle is not None:
                        handle.close()

                _emit_message(
                    sink,
                    f"  读取&精炼 / Read&Refine: {total_raw:,} 点/points, "
                    f"{vehicle_count:,} 车辆/vehicles ({elapsed_read:.1f}s)",
                )
                stats["source_points"] = total_raw
                _emit_message(sink, f"  排除 / Excluded: {total_excluded:,}")
                _emit_message(sink, f"  注入 / Injected: {total_injected:,}")
                stats["kept_trajectory_points"] = total_kept
                stats["injected_points"] = total_injected
                _emit_message(sink, f"  kept_trajectory_points: {total_kept:,} rows")
                _emit_message(sink, f"  injected_points: {total_injected:,} rows")
                source_fingerprint = calculate_source_fingerprint(
                    conn,
                    refine_id,
                    source_contract,
                )
                # Make authoritative rows visible to postgres_scanner while the
                # Run deliberately remains running.
                store.commit()
                staging = create_staging(
                    pc.paths.parquet_directory, "refine", refine_id, uuid4(),
                )
                descriptor = export_refine_parquet(
                    conn, db_config, staging, pc.runtime, source_fingerprint,
                    data_root=pc.paths.data_root,
                    postgres_extension_path=pc.paths.duckdb_postgres_extension_path,
                )
                # Close the exporter's transaction before taking an independent
                # post-export source view.
                store.rollback()
                confirmed_fingerprint = calculate_source_fingerprint(
                    conn, refine_id, source_contract,
                )
                if confirmed_fingerprint != source_fingerprint:
                    raise RuntimeError("Refine source changed during Parquet export")
                publish(staging)
                refine_published = True
                store.complete_refine_with_artifact(
                    refine_id, descriptor, confirmed_fingerprint,
                )
                store.commit()
            except Exception:
                if staging is not None and not refine_published:
                    with suppress(Exception):
                        abandon_staging(staging)
                _fail_running_refine(store, refine_id)
                raise

            refine_elapsed = _log_stage_finish(
                "refine",
                refine_started,
                refine_id=refine_id,
            )
            live_refine.stop(elapsed=refine_elapsed)

        if reuse_refine:
            _emit_message(sink, f"  排除 / Excluded: {total_excluded:,}")
            _emit_message(sink, f"  注入 / Injected: {total_injected:,}")

        detect_id = store.start_detect_run(
            refine_id=refine_id,
            parameters=config_parameters,
        )
        stats["detect_id"] = detect_id
        store.commit()

        detect_stage = (
            "Detect: 交叉口检测 / Intersection Detection "
            "(单次加载 + 全局索引 / Shared workspace + global index)"
        )
        intersect_started = _log_stage_start(
            "detect",
            refine_id=refine_id,
            detect_id=detect_id,
        )
        live_intersect = EventProgressAdapter(detect_stage, sink, completion_pending=True)
        live_intersect.start()
        detect_context = DetectRuntimeContext(
            query_active_from_db=True,
            db_table=source_table,
            kept_table="kept_trajectory_points",
            source_table=source_table,
            refine_id=refine_id,
            live_status=live_intersect,
        )
        detect_completed = False
        detect_staging = None
        detect_published = False
        iresult = None
        try:
            detect_data_root = pc.paths.data_root
            detect_compute_started = time.perf_counter()
            with store.connection.cursor() as cur:
                registered = find_refine_artifact(cur, refine_id)
            reader = RefineDatasetReader(
                pc.paths.parquet_directory, registered.descriptor, registered.state, pc.runtime,
                verify_sha256=pc.resources.verify_artifact_sha256,
                data_root=detect_data_root,
            )
            with reader:
                iresult = detect_intersections(
                    conn=store.connection,
                    config=pc.detect,
                    context=detect_context,
                    refine_reader=reader,
                    runtime=pc.runtime,
                    data_root=detect_data_root,
                )
            detect_compute_elapsed = time.perf_counter() - detect_compute_started
            # psycopg starts a transaction for the Detect SELECTs. The result
            # no longer depends on that DB cursor, so discard its snapshot before
            # opening the short, atomic Detect persistence transaction.
            store.rollback()
            stats["detect_compute_s"] = round(detect_compute_elapsed, 6)
            log_info(
                "[stage-phase] stage=detect phase=detect_compute "
                f"elapsed_s={detect_compute_elapsed:.3f}"
            )
            live_intersect.update(
                detail="detect_compute",
                elapsed=detect_compute_elapsed,
            )

            _emit_message(sink, f"  转向事件 / Turn events: {iresult.report.turn_event_count:,}")
            _emit_message(sink, f"  热点事件 / Hotspot events: {iresult.report.hotspot_event_count:,}")
            _emit_message(sink, f"  候选交叉口 / Clusters: {iresult.report.cluster_count:,}")
            _emit_message(
                sink,
                f"  完整交叉口 / Complete intersections: {iresult.report.complete_intersection_count:,}",
            )

            detect_persist_started = time.perf_counter()
            with operation_heartbeat(
                "Detect：写入结果",
                emit=lambda message: live_intersect.update(
                    detail=message, elapsed=time.perf_counter() - intersect_started,
                ),
            ):
                persisted_detect = store.persist_detect(
                    detect_id,
                    iresult,
                    runtime=pc.runtime,
                    data_root=detect_data_root,
                    batch_size=pc.runtime.db_insert_batch,
                )
            # Persistence is not complete until the run-level validator and
            # every deferred FK/constraint have passed at COMMIT.  Keep that
            # work inside ``detect_persist_s`` together with checkpoint export
            # and publication; this metric covers the full durable Detect path.
            with operation_heartbeat(
                "Detect：结果行校验与事务提交",
                emit=lambda message: live_intersect.update(
                    detail=message, elapsed=time.perf_counter() - intersect_started,
                ),
            ):
                store.commit()
            iresult.close()
            iresult = None

            # The authoritative Detect rows are committed while the Run stays
            # running.  Export from that stable checkpoint, then independently
            # re-read the canonical Refine/raw-source fingerprint before the
            # irreversible atomic rename.
            detect_source_contract = validate_source_table(conn, str(source_table))
            source_fingerprint = calculate_source_fingerprint(
                conn, str(refine_id), detect_source_contract,
            )
            detect_staging = create_staging(
                pc.paths.parquet_directory, "detect", detect_id, uuid4(),
            )
            detect_descriptor = export_detect_parquet(
                conn, db_config, detect_staging, pc.runtime, source_fingerprint,
                data_root=pc.paths.data_root,
                postgres_extension_path=pc.paths.duckdb_postgres_extension_path,
            )
            # Close the exporter's psycopg snapshot before checking a new view.
            store.rollback()
            confirmed_fingerprint = calculate_source_fingerprint(
                conn, str(refine_id), detect_source_contract,
            )
            if confirmed_fingerprint != source_fingerprint:
                raise RuntimeError("Detect source changed during Parquet export")
            publish(detect_staging)
            detect_published = True
            with operation_heartbeat(
                "Detect：制品登记、完整性校验与事务提交",
                emit=lambda message: live_intersect.update(
                    detail=message, elapsed=time.perf_counter() - intersect_started,
                ),
            ):
                store.complete_detect_with_artifact(detect_id, detect_descriptor)
                store.commit()
            detect_completed = True
            detect_persist_elapsed = time.perf_counter() - detect_persist_started
            stats["detect_persist_s"] = round(detect_persist_elapsed, 6)
            stats["resolved_sequence_s"] = round(
                persisted_detect.resolved_sequence_elapsed_s,
                6,
            )
            log_info(
                "[stage-phase] stage=detect phase=detect_persist "
                f"elapsed_s={detect_persist_elapsed:.3f} "
                "resolved_sequence_s="
                f"{persisted_detect.resolved_sequence_elapsed_s:.3f}"
            )
            live_intersect.update(
                detail="detect_persist",
                elapsed=detect_persist_elapsed,
            )
            stats.update(
                {
                    "tracks_cross": persisted_detect.tracks_cross_count,
                    "resolved_subtrack_points": persisted_detect.resolved_point_count,
                    "turn_events": persisted_detect.turn_event_count,
                    "candidate_intersections": persisted_detect.candidate_intersection_count,
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
                f"  candidate_entry_exit_points: {persisted_detect.candidate_entry_exit_point_count:,} rows",
            )
            _emit_message(sink, f"  intersections: {persisted_detect.intersection_count:,} rows")
            _emit_message(sink, f"  entry_exit_points: {persisted_detect.entry_exit_point_count:,} rows")

        except (Exception, KeyboardInterrupt):
            store.rollback()
            if detect_staging is not None and not detect_published:
                with suppress(Exception):
                    abandon_staging(detect_staging)
            # Once the atomic Detect transaction has committed, later output
            # or progress-rendering failures must not rewrite an immutable
            # completed run as failed.
            if not detect_completed:
                store.finish_detect(detect_id, status="failed")
                store.commit()
            raise
        finally:
            if iresult is not None:
                iresult.close()
        intersect_elapsed = _log_stage_finish(
            "detect",
            intersect_started,
            refine_id=refine_id,
            detect_id=detect_id,
        )
        live_intersect.stop(elapsed=intersect_elapsed)

        clip_started = _log_stage_start(
            "clip",
            refine_id=refine_id,
            detect_id=detect_id,
        )
        sink(PipelineEvent.stage_start("Clip: 路段切分 / Track clipping"))
        clip_config = pc.clip
        clip_assembly_started = time.perf_counter()
        clip_result = store.load_clip(
            detect_id, publication_root=pc.paths.parquet_directory,
            data_root=pc.paths.data_root, runtime=pc.runtime,
            config=clip_config,
            verify_sha256=pc.resources.verify_artifact_sha256,
        )
        close_clips = getattr(clip_result.clips, "close", None)
        if close_clips is not None:
            resources.callback(close_clips)
        stats["clip_assembly_s"] = round(
            time.perf_counter() - clip_assembly_started,
            6,
        )
        _add_clip_stats(stats, clip_result)
        _emit_report(sink, "  Clip 报告 / Clip report", clip_result.report.as_dict())
        clip_elapsed = _log_stage_finish(
            "clip",
            clip_started,
            refine_id=refine_id,
            detect_id=detect_id,
        )
        sink(
            PipelineEvent.stage_finish(
                "Clip: 路段切分 / Track clipping",
                elapsed=clip_elapsed,
            )
        )

        fitting_config = pc.fitting
        fit_id = store.start_fit_run(
            detect_id=detect_id,
            clip_config=clip_config,
            fitting_config=fitting_config,
            parameters=config_parameters,
        )
        stats["fit_id"] = fit_id
        store.commit()
        fitting_started = _log_stage_start(
            "fitting",
            refine_id=refine_id,
            detect_id=detect_id,
            fit_id=fit_id,
        )
        fitting_stage = "Fitting: 中心线拟合 / Centerline fitting"
        sink(PipelineEvent.stage_start(fitting_stage))
        fit_completed = False
        try:
            fit_compute_started = time.perf_counter()
            fit_bundle = fit_centerline_variants(clip_result, config=fitting_config)
            fit_compute_elapsed = time.perf_counter() - fit_compute_started
            log_info(
                "[stage-phase] stage=fitting phase=fit_compute "
                f"elapsed_s={fit_compute_elapsed:.3f}"
            )
            sink(PipelineEvent.stage_progress(
                fitting_stage,
                detail="fit_compute",
                elapsed=fit_compute_elapsed,
            ))

            fit_persist_started = time.perf_counter()
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
            with operation_heartbeat(
                "Fitting：完整性校验与事务提交",
                emit=lambda message: sink(PipelineEvent.stage_progress(
                    fitting_stage, detail=message,
                    elapsed=time.perf_counter() - fitting_started,
                )),
            ):
                store.finish_fit(fit_id, status="completed", report=report)
                store.commit()
            fit_completed = True
            fit_persist_elapsed = time.perf_counter() - fit_persist_started
            log_info(
                "[stage-phase] stage=fitting phase=fit_persist "
                f"elapsed_s={fit_persist_elapsed:.3f}"
            )
            sink(PipelineEvent.stage_progress(
                fitting_stage,
                detail="fit_persist",
                elapsed=fit_persist_elapsed,
            ))
        except (psycopg.Error, RuntimeError, ValueError):
            store.rollback()
            if not fit_completed:
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
        fitting_elapsed = _log_stage_finish(
            "fitting",
            fitting_started,
            refine_id=refine_id,
            detect_id=detect_id,
            fit_id=fit_id,
        )
        sink(
            PipelineEvent.stage_finish(
                fitting_stage,
                elapsed=fitting_elapsed,
            )
        )

        network_started = _log_stage_start(
            "network",
            refine_id=refine_id,
            detect_id=detect_id,
            fit_id=fit_id,
        )
        sink(PipelineEvent.stage_start("Network: 路网组装 / Network assembly"))
        network_result = build_network(
            fit_bundle.combined,
            clip_result.intersections,
            config=pc.network,
        )
        _add_network_stats(stats, network_result)
        _emit_report(
            sink,
            "  Network 报告 / Network report",
            network_result.report.as_dict(),
        )
        network_elapsed = _log_stage_finish(
            "network",
            network_started,
            refine_id=refine_id,
            detect_id=detect_id,
            fit_id=fit_id,
        )
        sink(
            PipelineEvent.stage_finish(
                "Network: 路网组装 / Network assembly",
                elapsed=network_elapsed,
            )
        )

        _emit_message(sink, "\n  提交 / Committed.")

    elapsed = time.perf_counter() - t_start
    stats["_elapsed_s"] = round(elapsed, 6)
    _emit_message(sink, f"\n── 完成 / Done ({elapsed:.1f}s) ──")
    return PipelineResult(stats=stats)


def _flush_refine_batch(
    store: PipelineStore,
    *,
    refine_id: str,
    pending_excluded: list,
    pending_subtracks: list[RefinementSubtrack],
    subtrack_index: int,
    batch_size: int,
    spatial_context: SpatialContext,
) -> tuple[int, int]:
    """Flush one batch of frozen Refine outputs."""
    result = store.persist_refine_batch(
        refine_id,
        pending_excluded,
        pending_subtracks,
        subtrack_index,
        batch_size=batch_size,
        spatial_context=spatial_context,
    )
    return result.next_subtrack_index, result.kept_count


def _should_flush_refine_batch(
    *,
    pending_excluded_count: int,
    pending_point_count: int,
    vehicle_count: int,
    runtime: RuntimeConfig,
) -> bool:
    """Bound Refine pending output between complete vehicle groups."""
    return (
        pending_excluded_count >= runtime.excluded_flush_threshold
        or pending_point_count >= runtime.refine_flush_point_threshold
        or vehicle_count % runtime.mark_flush_every_n == 0
    )


def _emit_message(sink: EventSink, message: str) -> None:
    sink(PipelineEvent.message_event(message))


def _log_stage_start(stage: str, **run_ids: object) -> float:
    """Log one stage boundary and return its monotonic start time."""
    started_at = time.perf_counter()
    context = " ".join(f"{name}={value}" for name, value in run_ids.items())
    log_info(f"[stage-start] stage={stage} {context}")
    return started_at


def _log_stage_finish(stage: str, started_at: float, **run_ids: object) -> float:
    """Log a completed stage with monotonic elapsed time and return the duration."""
    elapsed = time.perf_counter() - started_at
    context = " ".join(f"{name}={value}" for name, value in run_ids.items())
    log_info(
        f"[stage-finish] stage={stage} {context} "
        f"status=completed elapsed_s={elapsed:.3f}"
    )
    return elapsed


def _emit_report(sink: EventSink, title: str, report: dict[str, object]) -> None:
    _emit_message(sink, title)
    for key, value in report.items():
        rendered = (
            f"{value:,}"
            if isinstance(value, int) and not isinstance(value, bool)
            else str(value)
        )
        _emit_message(sink, f"  {key}: {rendered}")


def _add_clip_stats(
    stats: dict[str, float | int | str | list[int]],
    result: ClipResult,
) -> None:
    stats.update(
        {
            f"clip_{name}": value
            for name, value in result.report.as_dict().items()
            if isinstance(value, int)
        }
    )


def _add_fit_stats(
    stats: dict[str, float | int | str | list[int]],
    result: FitResult,
) -> None:
    stats.update(
        {
            f"fitting_{name}": value
            for name, value in result.report.as_dict().items()
            if isinstance(value, int)
        }
    )


def _add_network_stats(
    stats: dict[str, float | int | str | list[int]],
    result: NetworkResult,
) -> None:
    stats.update(
        {
            f"network_{name}": value
            for name, value in result.report.as_dict().items()
            if isinstance(value, int)
        }
    )
