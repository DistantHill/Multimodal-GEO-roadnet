"""Typed events emitted by the pipeline core.

The core pipeline can depend on this module without depending on terminal
rendering.  A CLI adapter can render the same events for an interactive user.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class PipelineEventKind(StrEnum):
    """Kinds of progress information a pipeline may emit."""

    MESSAGE = "message"
    STAGE_START = "stage_start"
    STAGE_PROGRESS = "stage_progress"
    STAGE_FINISH = "stage_finish"


@dataclass(frozen=True, slots=True)
class PipelineEvent:
    """One typed pipeline message or stage lifecycle update.

    ``stage`` is populated for stage events.  ``progress`` is a fraction in
    the range understood by a console adapter; ``None`` leaves the current
    progress unchanged.  The remaining stage fields are intentionally
    explicit rather than an untyped context dictionary.
    """

    kind: PipelineEventKind
    message: str = ""
    stage: str | None = None
    progress: float | None = None
    detail: str = ""
    sub_detail: str = ""
    elapsed: float | None = None
    throughput: str = ""

    @classmethod
    def message_event(cls, message: str) -> PipelineEvent:
        """Create an ordinary output message event."""

        return cls(kind=PipelineEventKind.MESSAGE, message=message)

    @classmethod
    def stage_start(cls, stage: str) -> PipelineEvent:
        """Create a stage-start event."""

        return cls(kind=PipelineEventKind.STAGE_START, stage=stage)

    @classmethod
    def stage_progress(
        cls,
        stage: str,
        *,
        progress: float | None = None,
        detail: str = "",
        sub_detail: str = "",
        elapsed: float | None = None,
        throughput: str = "",
    ) -> PipelineEvent:
        """Create a stage-progress event."""

        return cls(
            kind=PipelineEventKind.STAGE_PROGRESS,
            stage=stage,
            progress=progress,
            detail=detail,
            sub_detail=sub_detail,
            elapsed=elapsed,
            throughput=throughput,
        )

    @classmethod
    def stage_finish(
        cls,
        stage: str,
        *,
        progress: float | None = None,
        detail: str = "",
        sub_detail: str = "",
        elapsed: float | None = None,
        throughput: str = "",
        message: str = "",
    ) -> PipelineEvent:
        """Create a stage-finish event."""

        return cls(
            kind=PipelineEventKind.STAGE_FINISH,
            stage=stage,
            progress=progress,
            detail=detail,
            sub_detail=sub_detail,
            elapsed=elapsed,
            throughput=throughput,
            message=message,
        )


class EventSink(Protocol):
    """Callable boundary consumed by pipeline orchestration."""

    def __call__(self, event: PipelineEvent, /) -> None:
        ...


class EventProgressAdapter:
    """Adapt the legacy live-status calls to typed pipeline events."""

    def __init__(self, stage: str, emit: Callable[[PipelineEvent], None]) -> None:
        self._stage = stage
        self._emit = emit

    def start(self) -> None:
        """Emit the start event expected by a console reporter."""

        self._emit(PipelineEvent.stage_start(self._stage))

    def update(
        self,
        progress: float | None = None,
        detail: str = "",
        sub_detail: str = "",
        elapsed: float | None = None,
        throughput: str = "",
        stage: str | None = None,
    ) -> None:
        """Preserve the ``LiveStatus.update`` call shape at the core seam."""

        event_stage = stage or self._stage
        self._emit(
            PipelineEvent.stage_progress(
                event_stage,
                progress=progress,
                detail=detail,
                sub_detail=sub_detail,
                elapsed=elapsed,
                throughput=throughput,
            )
        )

    def stop(self) -> None:
        """Emit the stage-finish event expected by a console reporter."""

        self._emit(PipelineEvent.stage_finish(self._stage))
