"""Database-backed execution boundary for the Fitting stage."""

from __future__ import annotations

import time
from typing import Any

import psycopg

from .db.pipeline_store import PipelineStore
from .fitting import fit_centerline_variants
from .heartbeat import operation_heartbeat
from .logger import log_info
from .models import ClipConfig, FittingConfig
from .pipeline_events import EventSink, PipelineEvent
from .stage_support import emit_report, log_stage_finish, log_stage_start
from .stage_types import (
    ClipStageResult,
    DetectStageRef,
    FittingStageRef,
    FittingStageResult,
)


_STAGE_LABEL = "Fitting: 中心线拟合 / Centerline fitting"


def run_fitting_stage(
    *,
    store: PipelineStore,
    detect: DetectStageRef,
    clip: ClipStageResult,
    clip_config: ClipConfig,
    fitting_config: FittingConfig,
    parameters: dict[str, Any],
    emit: EventSink,
) -> FittingStageResult:
    fit_id = store.start_fit_run(
        detect_id=detect.detect_id,
        clip_config=clip_config,
        fitting_config=fitting_config,
        parameters=parameters,
    )
    store.commit()
    started_at = log_stage_start(
        "fitting",
        refine_id=detect.refine.refine_id,
        detect_id=detect.detect_id,
        fit_id=fit_id,
    )
    emit(PipelineEvent.stage_start(_STAGE_LABEL))
    completed = False
    try:
        compute_started = time.perf_counter()
        bundle = fit_centerline_variants(clip.result, config=fitting_config)
        compute_elapsed = time.perf_counter() - compute_started
        log_info(
            "[stage-phase] stage=fitting phase=fit_compute "
            f"elapsed_s={compute_elapsed:.3f}"
        )
        emit(PipelineEvent.stage_progress(
            _STAGE_LABEL, detail="fit_compute", elapsed=compute_elapsed,
        ))

        persist_started = time.perf_counter()
        centerline_ids = store.persist_fit_bundle(
            fit_id, detect.detect_id, bundle, clip.result,
        )
        report = {
            "combined": {
                "report": bundle.combined.report.as_dict(),
                "issues": [issue.as_dict() for issue in bundle.combined.issues],
            },
            "a_to_b": {
                "report": bundle.a_to_b.report.as_dict(),
                "issues": [issue.as_dict() for issue in bundle.a_to_b.issues],
            },
            "b_to_a": {
                "report": bundle.b_to_a.report.as_dict(),
                "issues": [issue.as_dict() for issue in bundle.b_to_a.issues],
            },
        }
        with operation_heartbeat(
            "Fitting：完整性校验与事务提交",
            emit=lambda message: emit(PipelineEvent.stage_progress(
                _STAGE_LABEL,
                detail=message,
                elapsed=time.perf_counter() - started_at,
            )),
        ):
            store.finish_fit(fit_id, status="completed", report=report)
            store.commit()
        completed = True
        persist_elapsed = time.perf_counter() - persist_started
        log_info(
            "[stage-phase] stage=fitting phase=fit_persist "
            f"elapsed_s={persist_elapsed:.3f}"
        )
        emit(PipelineEvent.stage_progress(
            _STAGE_LABEL, detail="fit_persist", elapsed=persist_elapsed,
        ))
    except (psycopg.Error, RuntimeError, ValueError):
        store.rollback()
        if not completed:
            store.finish_fit(fit_id, status="failed")
            store.commit()
        raise

    emit_report(emit, "  Fitting 报告 / Fitting report", bundle.combined.report.as_dict())
    elapsed = log_stage_finish(
        "fitting",
        started_at,
        refine_id=detect.refine.refine_id,
        detect_id=detect.detect_id,
        fit_id=fit_id,
    )
    emit(PipelineEvent.stage_finish(_STAGE_LABEL, elapsed=elapsed))
    return FittingStageResult(
        ref=FittingStageRef(fit_id, detect),
        bundle=bundle,
        centerline_ids=tuple(centerline_ids),
        compute_elapsed_s=compute_elapsed,
        persist_elapsed_s=persist_elapsed,
        elapsed_s=elapsed,
    )
