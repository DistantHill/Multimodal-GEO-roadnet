"""Database-backed execution boundary for the Refine stage."""

from __future__ import annotations

import time
from contextlib import suppress
from typing import Any
from uuid import uuid4

from .artifact_publication import StagingDataset, abandon_staging, create_staging, publish
from .config import ParserConfig, PathsConfig, PostgresConfig
from .db.pipeline_store import PipelineStore
from .db.source_table import (
    calculate_source_fingerprint,
    source_order_query,
    validate_source_table,
)
from .models import (
    ExcludedPoint,
    GeneratedReason,
    PostgresSource,
    RefineConfig,
    RefinementSubtrack,
    RuntimeConfig,
    SpatialContext,
)
from .parser import StreamVehicleSource, get_database
from .pipeline_events import EventProgressAdapter, EventSink
from .projection import CoordinateTransformer
from .refine import refine_vehicle
from .refine_parquet import export_refine_parquet
from .stage_support import emit_message, log_stage_finish, log_stage_start
from .stage_types import RefineStageRef, RefineStageResult


_STAGE_LABEL = "Parser + Refine: 流式读取&精炼 / Streaming read & refine"


def load_completed_refine_ref(
    *,
    store: PipelineStore,
    refine_id: str,
) -> RefineStageRef:
    record = store.get_refine_run(refine_id)
    if record.status != "completed":
        raise ValueError(f"Refine Run {refine_id} is not completed")
    return RefineStageRef(refine_id, record.source_table, record.spatial_context)


def run_refine_stage(
    *,
    store: PipelineStore,
    source_table: str,
    spatial_context: SpatialContext,
    postgres_config: PostgresConfig,
    parser_config: ParserConfig,
    refine_config: RefineConfig,
    runtime_config: RuntimeConfig,
    paths_config: PathsConfig,
    parameters: dict[str, Any],
    emit: EventSink,
) -> RefineStageResult:
    """Execute, publish, and complete one new Refine Run."""
    source_contract = validate_source_table(store.connection, source_table)
    source = PostgresSource(
        config=postgres_config,
        query=source_order_query(source_contract),
        source_id=source_table,
        primary_key_field="id",
    )
    refine_id = store.start_refine_run(
        source_table=source_table,
        parameters=parameters,
        spatial_context=spatial_context,
    )
    store.commit()

    started_at = log_stage_start("refine", refine_id=refine_id)
    progress = EventProgressAdapter(_STAGE_LABEL, emit)
    progress.start()
    emit_message(
        emit,
        f"  数据源 / Source: postgresql://{postgres_config.database}/{source_table}",
    )

    staging: StagingDataset | None = None
    published = False
    try:
        counts = _stream_and_persist(
            store=store,
            source=source,
            refine_id=refine_id,
            spatial_context=spatial_context,
            parser_config=parser_config,
            refine_config=refine_config,
            runtime_config=runtime_config,
            progress=progress,
            emit=emit,
        )
        (
            source_count,
            kept_count,
            excluded_count,
            injected_count,
            vehicle_count,
            read_elapsed,
        ) = counts
        emit_message(
            emit,
            f"  读取&精炼 / Read&Refine: {source_count:,} 点/points, "
            f"{vehicle_count:,} 车辆/vehicles ({read_elapsed:.1f}s)",
        )
        emit_message(emit, f"  排除 / Excluded: {excluded_count:,}")
        emit_message(emit, f"  注入 / Injected: {injected_count:,}")
        emit_message(emit, f"  kept_trajectory_points: {kept_count:,} rows")
        emit_message(emit, f"  injected_points: {injected_count:,} rows")

        fingerprint = calculate_source_fingerprint(
            store.connection, refine_id, source_contract,
        )
        store.commit()
        staging = create_staging(
            paths_config.parquet_directory, "refine", refine_id, uuid4(),
        )
        descriptor = export_refine_parquet(
            store.connection,
            postgres_config,
            staging,
            runtime_config,
            fingerprint,
            data_root=paths_config.data_root,
            postgres_extension_path=paths_config.duckdb_postgres_extension_path,
        )
        store.rollback()
        confirmed_fingerprint = calculate_source_fingerprint(
            store.connection, refine_id, source_contract,
        )
        if confirmed_fingerprint != fingerprint:
            raise RuntimeError("Refine source changed during Parquet export")
        publish(staging)
        published = True
        store.complete_refine_with_artifact(
            refine_id, descriptor, confirmed_fingerprint,
        )
        store.commit()
    except Exception:
        if staging is not None and not published:
            with suppress(Exception):
                abandon_staging(staging)
        _fail_running_refine(store, refine_id)
        raise

    elapsed = log_stage_finish("refine", started_at, refine_id=refine_id)
    progress.stop(elapsed=elapsed)
    return RefineStageResult(
        ref=RefineStageRef(refine_id, source_table, spatial_context),
        source_point_count=source_count,
        kept_point_count=kept_count,
        excluded_point_count=excluded_count,
        injected_point_count=injected_count,
        vehicle_count=vehicle_count,
        artifact=descriptor,
        elapsed_s=elapsed,
    )


def _stream_and_persist(
    *,
    store: PipelineStore,
    source: PostgresSource,
    refine_id: str,
    spatial_context: SpatialContext,
    parser_config: ParserConfig,
    refine_config: RefineConfig,
    runtime_config: RuntimeConfig,
    progress: EventProgressAdapter,
    emit: EventSink,
) -> tuple[int, int, int, int, int, float]:
    handle = get_database(
        source,
        field_config=parser_config.fields,
        runtime=runtime_config,
    )
    stream: StreamVehicleSource | None = None
    try:
        count_started = time.perf_counter()
        total_rows = handle.count_rows()
        emit_message(
            emit,
            f"  总行数 / Total rows: {total_rows:,} "
            f"({time.perf_counter() - count_started:.1f}s)",
        )
        source_count = injected_count = excluded_count = kept_count = vehicle_count = 0
        pending_excluded: list[ExcludedPoint] = []
        pending_subtracks: list[RefinementSubtrack] = []
        pending_point_count = 0
        subtrack_index = 1
        transformer = CoordinateTransformer(spatial_context)
        run_bounds: tuple[float, float, float, float] | None = None
        read_started = time.perf_counter()
        last_update = read_started
        stream = StreamVehicleSource(handle)

        for vehicle_points in stream.iter_vehicle_tracks():
            if vehicle_points:
                bounds = (
                    min(point.lon for point in vehicle_points),
                    min(point.lat for point in vehicle_points),
                    max(point.lon for point in vehicle_points),
                    max(point.lat for point in vehicle_points),
                )
                run_bounds = _extend_bounds(run_bounds, bounds)
            source_count += len(vehicle_points)
            vehicle_count += 1
            result = refine_vehicle(
                vehicle_points,
                config=refine_config,
                spatial_context=spatial_context,
                _transformer=transformer,
            )
            injected_count += result.derived_counts.get(
                GeneratedReason.HEADING_ORTHOGONAL, 0,
            )
            pending_excluded.extend(result.excluded)
            excluded_count += len(result.excluded)
            pending_subtracks.extend(result.subtracks)
            pending_point_count += len(result.excluded) + sum(
                len(subtrack.points) for subtrack in result.subtracks
            )
            if _should_flush(
                pending_excluded_count=len(pending_excluded),
                pending_point_count=pending_point_count,
                vehicle_count=vehicle_count,
                runtime=runtime_config,
            ):
                subtrack_index, batch_kept = _flush(
                    store=store,
                    refine_id=refine_id,
                    pending_excluded=pending_excluded,
                    pending_subtracks=pending_subtracks,
                    subtrack_index=subtrack_index,
                    batch_size=runtime_config.db_insert_batch,
                    spatial_context=spatial_context,
                )
                kept_count += batch_kept
                pending_point_count = 0

            now = time.perf_counter()
            if (
                vehicle_count == 1
                or vehicle_count % runtime_config.progress_interval == 0
                or now - last_update >= runtime_config.progress_time_threshold_s
            ):
                elapsed = now - read_started
                progress.update(
                    progress=source_count / total_rows if total_rows > 0 else 0,
                    detail=f"{source_count:,}/{total_rows:,} 点, {vehicle_count:,} 车辆",
                    elapsed=elapsed,
                    throughput=f"{source_count / elapsed:.0f} 点/s" if elapsed > 0 else "",
                )
                last_update = now

        if run_bounds is not None:
            transformer.validate_bounds(run_bounds)
        _, batch_kept = _flush(
            store=store,
            refine_id=refine_id,
            pending_excluded=pending_excluded,
            pending_subtracks=pending_subtracks,
            subtrack_index=subtrack_index,
            batch_size=runtime_config.db_insert_batch,
            spatial_context=spatial_context,
        )
        kept_count += batch_kept
        elapsed = time.perf_counter() - read_started
        progress.update(
            progress=1.0,
            detail=f"{source_count:,}/{total_rows:,} 点, {vehicle_count:,} 车辆",
            elapsed=elapsed,
            throughput=f"{source_count / elapsed:.0f} 点/s" if elapsed > 0 else "",
        )
        return source_count, kept_count, excluded_count, injected_count, vehicle_count, elapsed
    finally:
        if stream is not None:
            stream.close()
        else:
            handle.close()


def _extend_bounds(
    current: tuple[float, float, float, float] | None,
    addition: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    if current is None:
        return addition
    return (
        min(current[0], addition[0]),
        min(current[1], addition[1]),
        max(current[2], addition[2]),
        max(current[3], addition[3]),
    )


def _should_flush(
    *, pending_excluded_count: int, pending_point_count: int,
    vehicle_count: int, runtime: RuntimeConfig,
) -> bool:
    return (
        pending_excluded_count >= runtime.excluded_flush_threshold
        or pending_point_count >= runtime.refine_flush_point_threshold
        or vehicle_count % runtime.mark_flush_every_n == 0
    )


def _flush(
    *, store: PipelineStore, refine_id: str,
    pending_excluded: list[ExcludedPoint],
    pending_subtracks: list[RefinementSubtrack], subtrack_index: int,
    batch_size: int, spatial_context: SpatialContext,
) -> tuple[int, int]:
    result = store.persist_refine_batch(
        refine_id,
        pending_excluded,
        pending_subtracks,
        subtrack_index,
        batch_size=batch_size,
        spatial_context=spatial_context,
    )
    return result.next_subtrack_index, result.kept_count


def _fail_running_refine(store: PipelineStore, refine_id: str) -> None:
    with suppress(Exception):
        store.rollback()
    with suppress(Exception):
        if store.get_refine_run(refine_id).status == "running":
            store.finish_refine(refine_id, status="failed")
            store.commit()
