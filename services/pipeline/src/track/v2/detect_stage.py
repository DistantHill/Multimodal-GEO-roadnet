"""Database-backed execution boundary for the Detect stage."""

from __future__ import annotations

import time
from contextlib import suppress
from typing import Any
from uuid import uuid4

from .artifact_publication import StagingDataset, abandon_staging, create_staging, publish
from .config import PathsConfig, PostgresConfig
from .db.artifact_registry import find_refine_artifact
from .db.pipeline_store import DetectPersistenceResult, PipelineStore
from .db.source_table import calculate_source_fingerprint, validate_source_table
from .detect import DetectRuntimeContext, IntersectConfig, IntersectResult, detect_intersections
from .detect_parquet_export import export_detect_parquet
from .heartbeat import operation_heartbeat
from .logger import log_info
from .models import RuntimeConfig
from .pipeline_events import EventProgressAdapter, EventSink
from .refine_parquet import RefineDatasetReader
from .stage_support import emit_message, log_stage_finish, log_stage_start
from .stage_types import DetectStageRef, DetectStageResult, RefineStageRef


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
) -> DetectStageResult:
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
        query_active_from_db=True,
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
        )
        with reader:
            connect_worker = None
            if detect_config.use_cross_track_candidates:
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

        source_contract = validate_source_table(store.connection, refine.source_table)
        fingerprint = calculate_source_fingerprint(
            store.connection, refine.refine_id, source_contract,
        )
        staging = create_staging(
            paths_config.parquet_directory, "detect", detect_id, uuid4(),
        )
        descriptor = export_detect_parquet(
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
            store.connection, refine.refine_id, source_contract,
        )
        if confirmed_fingerprint != fingerprint:
            raise RuntimeError("Detect source changed during Parquet export")
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
