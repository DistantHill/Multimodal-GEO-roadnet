"""Database-backed execution boundary for the Detect stage."""

from __future__ import annotations

import time
from contextlib import suppress
from dataclasses import replace
from typing import Any
from uuid import uuid4

from ..artifacts.publish_dataset import StagingDataset, abandon_staging, create_staging, publish
from ..config import PathsConfig, PostgresConfig
from ..db.artifact_registry import find_refine_artifact
from ..db.pipeline_store import DetectPersistenceResult, PipelineStore
from ..intersections.detect_intersections import DetectRuntimeContext, IntersectConfig, IntersectResult, detect_intersections
from ..intersections.results.export_to_parquet import export_detect_results_to_parquet
from ..telemetry.heartbeat import operation_heartbeat
from ..telemetry.console_logging import log_info
from ..models import RuntimeConfig
from ..orchestration.events import EventProgressAdapter, EventSink
from ..trajectories.results.read_parquet import RefineDatasetReader
from .support import emit_message, log_stage_finish, log_stage_start
from .types import DetectStageRef, DetectStageResult, RefineStageRef


_STAGE_LABEL = (
    "Detect: 交叉口检测 / Intersection Detection "
    "(转向 Parquet + 跨轨迹网格 / Turn Parquet + cross-track grids)"
)


def run_detect_stage(
    *,
    store: PipelineStore,
    postgres_config: PostgresConfig,
    refine: RefineStageRef,
    detect_config: IntersectConfig,
    runtime_config: RuntimeConfig,
    paths_config: PathsConfig,
    verify_sha256: bool,
    parameters: dict[str, Any],
    emit: EventSink,
    time_start=None, time_end=None,
    candidate_cache=None,
    bbox=None,
) -> DetectStageResult:
    from ..trajectories.results.read_parquet import validate_bbox
    bbox = validate_bbox(bbox)
    if bbox is not None and detect_config.candidates != ("turn_threshold",):
        raise ValueError("strict bbox currently supports only turn_threshold")
    from ..time_selection import validate_refine_time
    start = end = None
    requested_start = refine.time_start if time_start is None else time_start
    requested_end = refine.time_end if time_end is None else time_end
    if requested_start is not None or requested_end is not None:
        start, end = validate_refine_time(
            store, store.get_refine_run(refine.refine_id), requested_start, requested_end,
        )
        refine = replace(refine, time_start=start, time_end=end)
    parameters = dict(parameters)
    parameters["refine_time_selection"] = {
        "time_start": start.isoformat() if start else None,
        "time_end": end.isoformat() if end else None,
    }
    if bbox is not None:
        parameters["detect_input_bbox"] = list(bbox)
        parameters["detect_input_bbox_policy"] = "strict_contiguous_fragments"
    detect_id = store.start_detect_run(
        refine_id=refine.refine_id,
        parameters=parameters,
    )
    store.commit()
    started_at = log_stage_start(
        "detect", refine_id=refine.refine_id, detect_id=detect_id,
    )
    progress = EventProgressAdapter(_STAGE_LABEL, emit, completion_pending=True)
    progress.start()
    context = DetectRuntimeContext(
        db_table=refine.source_table,
        kept_table="kept_trajectory_points",
        source_table=refine.source_table,
        refine_id=refine.refine_id,
        live_status=progress,
    )
    completed = False
    published = False
    staging: StagingDataset | None = None
    result: IntersectResult | None = None
    try:
        compute_started = time.perf_counter()
        with store.connection.cursor() as cur:
            registered = find_refine_artifact(cur, refine.refine_id)
        reader = RefineDatasetReader(
            paths_config.parquet_directory,
            registered.descriptor,
            registered.state,
            runtime_config,
            verify_sha256=verify_sha256,
            data_root=paths_config.data_root,
            time_start=start, time_end=end,
            **({"bbox": bbox} if bbox is not None else {}),
        )
        with reader:
            connect_worker = None
            if detect_config.candidates:
                import psycopg

                worker_parameters = dict(store.connection.info.get_parameters())
                worker_parameters.pop("password", None)

                def connect_worker():
                    return psycopg.connect(
                        **worker_parameters, password=postgres_config.password,
                    )

            result = detect_intersections(
                conn=store.connection,
                connect_worker=connect_worker,
                config=detect_config,
                context=context,
                refine_reader=reader,
                runtime=runtime_config,
                data_root=paths_config.data_root,
                **({"candidate_cache": candidate_cache} if candidate_cache is not None else {}),
            )
        compute_elapsed = time.perf_counter() - compute_started
        store.rollback()
        log_info(
            "[stage-phase] stage=detect phase=detect_compute "
            f"elapsed_s={compute_elapsed:.3f}"
        )
        progress.update(detail="detect_compute", elapsed=compute_elapsed)
        emit_message(emit, f"  转向事件 / Turn events: {result.report.turn_event_count:,}")
        emit_message(emit, f"  热点事件 / Hotspot events: {result.report.hotspot_event_count:,}")
        emit_message(emit, f"  候选交叉口 / Clusters: {result.report.cluster_count:,}")
        emit_message(
            emit,
            "  完整交叉口 / Complete intersections: "
            f"{result.report.complete_intersection_count:,}",
        )

        persist_started = time.perf_counter()
        with operation_heartbeat(
            "Detect：写入结果",
            emit=lambda message: progress.update(
                detail=message, elapsed=time.perf_counter() - started_at,
            ),
        ):
            persisted = store.persist_detect(
                detect_id,
                result,
                runtime=runtime_config,
                data_root=paths_config.data_root,
                batch_size=runtime_config.db_insert_batch,
                **({"time_start": start, "time_end": end}
                   if start is not None or end is not None else {}),
            )
        with operation_heartbeat(
            "Detect：结果行校验与事务提交",
            emit=lambda message: progress.update(
                detail=message, elapsed=time.perf_counter() - started_at,
            ),
        ):
            store.commit()

        # Release calculation scratch before Parquet export starts.
        result.close()
        result = None

        source_identity = {"refine_id": refine.refine_id, "detect_id": detect_id}
        staging = create_staging(
            paths_config.parquet_directory, "detect", detect_id, uuid4(),
        )
        descriptor = export_detect_results_to_parquet(
            store.connection,
            postgres_config,
            staging,
            runtime_config,
            source_identity,
            data_root=paths_config.data_root,
            postgres_extension_path=paths_config.duckdb_postgres_extension_path,
            refine_descriptor=registered.descriptor,
            parquet_directory=paths_config.parquet_directory,
        )
        store.rollback()
        publish(staging)
        published = True
        with operation_heartbeat(
            "Detect：制品登记、完整性校验与事务提交",
            emit=lambda message: progress.update(
                detail=message, elapsed=time.perf_counter() - started_at,
            ),
        ):
            store.complete_detect_with_artifact(detect_id, descriptor)
            store.commit()
        completed = True
        persist_elapsed = time.perf_counter() - persist_started
        log_info(
            "[stage-phase] stage=detect phase=detect_persist "
            f"elapsed_s={persist_elapsed:.3f} "
            f"resolved_sequence_s={persisted.resolved_sequence_elapsed_s:.3f}"
        )
        progress.update(detail="detect_persist", elapsed=persist_elapsed)
        _emit_persisted_counts(emit, persisted)
    except (Exception, KeyboardInterrupt):
        store.rollback()
        if staging is not None and not published:
            with suppress(Exception):
                abandon_staging(staging)
        if not completed:
            store.finish_detect(detect_id, status="failed")
            store.commit()
        raise
    finally:
        if result is not None:
            result.close()

    elapsed = log_stage_finish(
        "detect", started_at, refine_id=refine.refine_id, detect_id=detect_id,
    )
    progress.stop(elapsed=elapsed)
    return DetectStageResult(
        ref=DetectStageRef(detect_id, refine),
        artifact=descriptor,
        persisted=persisted,
        compute_elapsed_s=compute_elapsed,
        persist_elapsed_s=persist_elapsed,
        elapsed_s=elapsed,
    )


def _emit_persisted_counts(
    emit: EventSink,
    persisted: DetectPersistenceResult,
) -> None:
    emit_message(emit, f"  tracks_cross: {persisted.tracks_cross_count:,} rows")
    emit_message(emit, f"  turn_events: {persisted.turn_event_count:,} rows")
    emit_message(
        emit,
        f"  candidate_intersections: {persisted.candidate_intersection_count:,} rows",
    )
    emit_message(
        emit,
        "  candidate_entry_exit_points: "
        f"{persisted.candidate_entry_exit_point_count:,} rows",
    )
    emit_message(emit, f"  intersections: {persisted.intersection_count:,} rows")
    emit_message(emit, f"  entry_exit_points: {persisted.entry_exit_point_count:,} rows")
