"""Console adapter for typed pipeline events."""

from __future__ import annotations

from collections.abc import Callable

from ..telemetry.console_logging import LiveStatus
from ..orchestration.events import PipelineEvent, PipelineEventKind


class PipelineReporter:
    """Render pipeline events as ordinary output and live stage panels."""

    def __init__(
        self,
        *,
        live_status_factory: Callable[[str], LiveStatus] | None = None,
        output: Callable[[str], None] | None = None,
    ) -> None:
        self._live_status_factory = (
            LiveStatus if live_status_factory is None else live_status_factory
        )
        self._output = print if output is None else output
        self._live_status: LiveStatus | None = None

    def __call__(self, event: PipelineEvent, /) -> None:
        """Allow the reporter to be passed directly as an ``EventSink``."""

        self.emit(event)

    def emit(self, event: PipelineEvent, /) -> None:
        """Render one event and propagate adapter errors to the caller."""

        if event.kind == PipelineEventKind.MESSAGE:
            self._output(event.message)
            return

        if event.kind == PipelineEventKind.STAGE_START:
            self._start_stage(event)
            return

        if event.kind == PipelineEventKind.STAGE_PROGRESS:
            if self._live_status is None:
                raise RuntimeError("stage progress received without an active stage")
            self._live_status.update(
                progress=event.progress,
                detail=event.detail,
                sub_detail=event.sub_detail,
                elapsed=event.elapsed,
                throughput=event.throughput,
                stage=event.stage,
            )
            return

        if event.kind == PipelineEventKind.STAGE_FINISH:
            try:
                if self._live_status is not None:
                    self._live_status.update(
                        progress=event.progress,
                        detail=event.detail,
                        sub_detail=event.sub_detail,
                        elapsed=event.elapsed,
                        throughput=event.throughput,
                        stage=event.stage,
                    )
            finally:
                self.close()
            if event.message:
                self._output(event.message)
            return

        raise ValueError(f"unsupported pipeline event kind: {event.kind!r}")

    def _start_stage(self, event: PipelineEvent) -> None:
        if self._live_status is not None:
            self.close()
        self._output(f"\n── {event.stage} ──")
        live_status = self._live_status_factory(event.stage or "")
        live_status.start()
        self._live_status = live_status

    def close(self) -> None:
        """Stop the active stage, if any; repeated calls are no-ops."""

        if self._live_status is None:
            return
        live_status = self._live_status
        live_status.stop()
        self._live_status = None
