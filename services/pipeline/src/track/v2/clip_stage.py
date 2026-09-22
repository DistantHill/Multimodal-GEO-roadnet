"""Execution and resource boundary for the Clip stage."""

from __future__ import annotations

import time

from .config import PathsConfig
from .db.pipeline_store import PipelineStore
from .models import ClipConfig, RuntimeConfig
from .pipeline_events import EventSink, PipelineEvent
from .stage_support import emit_report, log_stage_finish, log_stage_start
from .stage_types import ClipStageResult, DetectStageRef


_STAGE_LABEL = "Clip: 路段切分 / Track clipping"


def run_clip_stage(
    *,
    store: PipelineStore,
    detect: DetectStageRef,
    clip_config: ClipConfig,
    runtime_config: RuntimeConfig,
    paths_config: PathsConfig,
    verify_sha256: bool,
    emit: EventSink,
) -> ClipStageResult:
    started_at = log_stage_start(
        "clip",
        refine_id=detect.refine.refine_id,
        detect_id=detect.detect_id,
    )
    emit(PipelineEvent.stage_start(_STAGE_LABEL))
    assembly_started = time.perf_counter()
    result = store.load_clip(
        detect.detect_id,
        publication_root=paths_config.parquet_directory,
        data_root=paths_config.data_root,
        runtime=runtime_config,
        config=clip_config,
        verify_sha256=verify_sha256,
    )
    try:
        assembly_elapsed = time.perf_counter() - assembly_started
        emit_report(emit, "  Clip 报告 / Clip report", result.report.as_dict())
        elapsed = log_stage_finish(
            "clip",
            started_at,
            refine_id=detect.refine.refine_id,
            detect_id=detect.detect_id,
        )
        emit(PipelineEvent.stage_finish(_STAGE_LABEL, elapsed=elapsed))
        return ClipStageResult(result, assembly_elapsed, elapsed)
    except BaseException:
        close = getattr(result.clips, "close", None)
        if close is not None:
            close()
        raise
