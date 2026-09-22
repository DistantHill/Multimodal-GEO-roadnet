"""Shared event and timing helpers for Pipeline stage runners."""

from __future__ import annotations

import time
from collections.abc import Mapping

from .logger import log_info
from .pipeline_events import EventSink, PipelineEvent


def emit_message(emit: EventSink, message: str) -> None:
    emit(PipelineEvent.message_event(message))


def emit_report(
    emit: EventSink,
    title: str,
    report: Mapping[str, object],
) -> None:
    emit_message(emit, title)
    for key, value in report.items():
        rendered = (
            f"{value:,}"
            if isinstance(value, int) and not isinstance(value, bool)
            else str(value)
        )
        emit_message(emit, f"  {key}: {rendered}")


def log_stage_start(stage: str, **run_ids: object) -> float:
    started_at = time.perf_counter()
    context = " ".join(f"{name}={value}" for name, value in run_ids.items())
    log_info(f"[stage-start] stage={stage} {context}")
    return started_at


def log_stage_finish(
    stage: str,
    started_at: float,
    **run_ids: object,
) -> float:
    elapsed = time.perf_counter() - started_at
    context = " ".join(f"{name}={value}" for name, value in run_ids.items())
    log_info(
        f"[stage-finish] stage={stage} {context} "
        f"status=completed elapsed_s={elapsed:.3f}"
    )
    return elapsed
