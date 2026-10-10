"""Thin orchestration for the database-backed track.v2 pipeline."""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..stages.clip import run_clip_stage
from ..config import PipelineConfig, connect as pg_connect
from ..db.pipeline_store import PostgresPipelineStore
from ..db.schema import require_current_schema
from ..stages.detect import run_detect_stage
from ..stages.fitting import run_fitting_stage
from ..stages.network import run_network_stage
from .events import EventSink, PipelineEvent
from ..projection import create_spatial_context
from ..stages.refine import (
    load_completed_refine_ref,
    run_refine_stage,
)
from ..stages.support import emit_message
from ..stages.types import (
    ClipStageResult,
    DetectStageResult,
    FittingStageResult,
    NetworkStageResult,
    RefineStageRef,
    RefineStageResult,
)


@dataclass(frozen=True, slots=True)
class PipelineInput:
    """Inputs accepted by the database-backed pipeline runner."""

    source_table: str | None = None
    refine_id: str | None = None
    selection: object | None = None
    time_start: str | None = None
    time_end: str | None = None


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Persisted row counts and stage summary produced by one pipeline run."""

    stats: dict[str, float | int | str | list[int]]


def _noop_event_sink(_event: PipelineEvent) -> None:
    """Default sink for non-CLI callers."""


def run_pipeline(
    pipeline_input: PipelineInput,
    config: PipelineConfig,
    *,
    emit: EventSink | None = None,
) -> PipelineResult:
    """Run the stage sequence and aggregate its compatibility result."""
    sink: EventSink = _noop_event_sink if emit is None else emit
    _validate_pipeline_input(pipeline_input)
    from ..time_selection import time_bounds
    from ..experiments.config import TimeSelection
    start, end = time_bounds(pipeline_input.time_start, pipeline_input.time_end)
    if start is not None or end is not None:
        if pipeline_input.selection is not None:
            raise ValueError("use either selection or time_start/time_end")
        if pipeline_input.source_table is not None:
            if start is None or end is None:
                raise ValueError("new Refine requires both time_start and time_end")
            from dataclasses import replace
            pipeline_input = replace(pipeline_input, selection=TimeSelection(start, end))
    parameters = config.snapshot_parameters()
    if pipeline_input.selection is not None:
        parameters["source_selection"] = pipeline_input.selection.snapshot()
    started_at = time.perf_counter()

    with pg_connect(config.postgres) as conn:
        require_current_schema(conn)
        store = PostgresPipelineStore(conn)
        refine_ref, refine_result = _resolve_refine(
            pipeline_input=pipeline_input,
            config=config,
            store=store,
            parameters=parameters,
            emit=sink,
        )
        detect = run_detect_stage(
            store=store,
            postgres_config=config.postgres,
            refine=refine_ref,
            detect_config=config.detect,
            runtime_config=config.runtime,
            paths_config=config.paths,
            verify_sha256=config.resources.verify_artifact_sha256,
            parameters=parameters,
            emit=sink,
        )

        with run_clip_stage(
            store=store,
            detect=detect.ref,
            clip_config=config.clip,
            runtime_config=config.runtime,
            paths_config=config.paths,
            verify_sha256=config.resources.verify_artifact_sha256,
            emit=sink,
        ) as clip:
            fitting = run_fitting_stage(
                store=store,
                detect=detect.ref,
                clip=clip,
                clip_config=config.clip,
                fitting_config=config.fitting,
                parameters=parameters,
                emit=sink,
            )
            network = run_network_stage(
                fitting=fitting,
                intersections=clip.result.intersections,
                config=config.network,
                emit=sink,
            )

        emit_message(sink, "\n  提交 / Committed.")

    elapsed_s = time.perf_counter() - started_at
    result = _build_pipeline_result(
        refine_ref=refine_ref,
        refine_result=refine_result,
        detect=detect,
        clip=clip,
        fitting=fitting,
        network=network,
        elapsed_s=elapsed_s,
    )
    emit_message(sink, f"\n── 完成 / Done ({elapsed_s:.1f}s) ──")
    return result


def _validate_pipeline_input(pipeline_input: PipelineInput) -> None:
    if sum(
        value is not None
        for value in (pipeline_input.source_table, pipeline_input.refine_id)
    ) != 1:
        raise ValueError("exactly one of source_table or refine_id is required")


def _resolve_refine(
    *,
    pipeline_input: PipelineInput,
    config: PipelineConfig,
    store: PostgresPipelineStore,
    parameters: dict[str, object],
    emit: EventSink,
) -> tuple[RefineStageRef, RefineStageResult | None]:
    if pipeline_input.refine_id is not None:
        record = store.get_refine_run(pipeline_input.refine_id)
        refine_ref = load_completed_refine_ref(
            store=store,
            refine_id=pipeline_input.refine_id,
            record=record,
            time_start=getattr(pipeline_input.selection, "time_start", pipeline_input.time_start),
            time_end=getattr(pipeline_input.selection, "time_end", pipeline_input.time_end),
        )
        # These historical messages mean Refine was not executed in this run.
        emit_message(emit, "  排除 / Excluded: 0")
        emit_message(emit, "  注入 / Injected: 0")
        return refine_ref, None

    assert pipeline_input.source_table is not None
    spatial_context = create_spatial_context(
        config.projection,
        processing_srid=config.projection.processing_srid,
    )
    result = run_refine_stage(
        store=store,
        source_table=pipeline_input.source_table,
        spatial_context=spatial_context,
        postgres_config=config.postgres,
        parser_config=config.parser,
        refine_config=config.refine,
        runtime_config=config.runtime,
        paths_config=config.paths,
        parameters=parameters,
        emit=emit,
        selection=pipeline_input.selection,
    )
    return result.ref, result


def _build_pipeline_result(
    *,
    refine_ref: RefineStageRef,
    refine_result: RefineStageResult | None,
    detect: DetectStageResult,
    clip: ClipStageResult,
    fitting: FittingStageResult,
    network: NetworkStageResult,
    elapsed_s: float,
) -> PipelineResult:
    persisted = detect.persisted
    stats: dict[str, float | int | str | list[int]] = {
        "refine_id": refine_ref.refine_id,
        "detect_id": detect.ref.detect_id,
        "fit_id": fitting.ref.fit_id,
        "source_srid": refine_ref.spatial_context.source_srid,
        "processing_srid": refine_ref.spatial_context.processing_srid,
        "detect_compute_s": round(detect.compute_elapsed_s, 6),
        "detect_persist_s": round(detect.persist_elapsed_s, 6),
        "resolved_sequence_s": round(persisted.resolved_sequence_elapsed_s, 6),
        "tracks_cross": persisted.tracks_cross_count,
        "resolved_subtrack_points": persisted.resolved_point_count,
        "turn_events": persisted.turn_event_count,
        "candidate_intersections": persisted.candidate_intersection_count,
        "candidate_entry_exit_points": persisted.candidate_entry_exit_point_count,
        "intersections": persisted.intersection_count,
        "entry_exit_points": persisted.entry_exit_point_count,
        "clip_assembly_s": round(clip.assembly_elapsed_s, 6),
        "centerlines": len(fitting.centerline_ids),
        "centerline_ids": list(fitting.centerline_ids),
        "_elapsed_s": round(elapsed_s, 6),
    }
    if refine_result is not None:
        stats.update(
            {
                "source_points": refine_result.source_point_count,
                "kept_trajectory_points": refine_result.kept_point_count,
                "injected_points": refine_result.injected_point_count,
            }
        )
    for prefix, report in (
        ("clip", clip.result.report),
        ("fitting", fitting.bundle.combined.report),
        ("network", network.result.report),
    ):
        stats.update(
            {
                f"{prefix}_{name}": value
                for name, value in report.as_dict().items()
                if isinstance(value, int)
            }
        )
    return PipelineResult(stats=stats)
