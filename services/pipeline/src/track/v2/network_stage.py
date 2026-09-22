"""Execution boundary for the Network stage."""

from __future__ import annotations

from collections.abc import Iterable

from .models import ClipIntersection, NetworkConfig
from .network import build_network
from .pipeline_events import EventSink, PipelineEvent
from .stage_support import emit_report, log_stage_finish, log_stage_start
from .stage_types import FittingStageResult, NetworkStageResult


_STAGE_LABEL = "Network: 路网组装 / Network assembly"


def run_network_stage(
    *,
    fitting: FittingStageResult,
    intersections: Iterable[ClipIntersection],
    config: NetworkConfig,
    emit: EventSink,
) -> NetworkStageResult:
    detect = fitting.ref.detect
    started_at = log_stage_start(
        "network",
        refine_id=detect.refine.refine_id,
        detect_id=detect.detect_id,
        fit_id=fitting.ref.fit_id,
    )
    emit(PipelineEvent.stage_start(_STAGE_LABEL))
    result = build_network(
        fitting.bundle.combined,
        intersections,
        config=config,
    )
    emit_report(emit, "  Network 报告 / Network report", result.report.as_dict())
    elapsed = log_stage_finish(
        "network",
        started_at,
        refine_id=detect.refine.refine_id,
        detect_id=detect.detect_id,
        fit_id=fitting.ref.fit_id,
    )
    emit(PipelineEvent.stage_finish(_STAGE_LABEL, elapsed=elapsed))
    return NetworkStageResult(result, elapsed)
