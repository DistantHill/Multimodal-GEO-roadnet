from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from track.v2.cli.pipeline_reporter import PipelineReporter
from track.v2.pipeline_events import (
    EventProgressAdapter,
    EventSink,
    PipelineEvent,
    PipelineEventKind,
)
from track.v2.stage_support import emit_report


class FakeLiveStatus:
    def __init__(self, stage: str, calls: list[tuple[object, ...]]) -> None:
        self.stage = stage
        self.calls = calls

    def start(self) -> None:
        self.calls.append(("start", self.stage))

    def update(self, **kwargs: object) -> None:
        self.calls.append(("update", self.stage, kwargs))

    def stop(self) -> None:
        self.calls.append(("stop", self.stage))


class PipelineEventTest(unittest.TestCase):
    def test_events_are_frozen_and_typed(self) -> None:
        event = PipelineEvent.stage_progress(
            "refine",
            progress=0.5,
            detail="10/20 points",
            elapsed=1.2,
            throughput="8 points/s",
        )

        self.assertEqual(event.kind, PipelineEventKind.STAGE_PROGRESS)
        self.assertEqual(event.stage, "refine")
        self.assertEqual(event.progress, 0.5)
        self.assertEqual(event.detail, "10/20 points")
        self.assertEqual(event.elapsed, 1.2)
        self.assertEqual(event.throughput, "8 points/s")
        with self.assertRaises(FrozenInstanceError):
            event.progress = 0.75  # type: ignore[misc]

    def test_event_sink_accepts_reporter(self) -> None:
        reporter = PipelineReporter(output=lambda _message: None)
        sink: EventSink = reporter
        sink(PipelineEvent.message_event("hello"))

    def test_progress_adapter_preserves_legacy_update_shape(self) -> None:
        events: list[PipelineEvent] = []
        adapter = EventProgressAdapter("detect", events.append)

        adapter.start()
        adapter.update(
            progress=0.5,
            detail="10/20 points",
            sub_detail="window 2",
            elapsed=1.2,
            throughput="8 points/s",
            stage="cross_track",
        )
        adapter.stop(elapsed=2.5)

        self.assertEqual(
            [event.kind for event in events],
            [
                PipelineEventKind.STAGE_START,
                PipelineEventKind.STAGE_PROGRESS,
                PipelineEventKind.STAGE_FINISH,
            ],
        )
        self.assertEqual(events[1].stage, "cross_track")
        self.assertEqual(events[1].detail, "10/20 points")
        self.assertEqual(events[1].sub_detail, "window 2")
        self.assertEqual(events[2].elapsed, 2.5)

    def test_report_messages_preserve_integer_formatting(self) -> None:
        events: list[PipelineEvent] = []
        emit_report(
            events.append,
            "Report",
            {"count": 1234, "enabled": True, "label": "ok"},
        )

        self.assertEqual(
            [event.message for event in events],
            ["Report", "  count: 1,234", "  enabled: True", "  label: ok"],
        )


class PipelineReporterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.outputs: list[str] = []
        self.statuses: list[FakeLiveStatus] = []

        def factory(stage: str) -> FakeLiveStatus:
            status = FakeLiveStatus(stage, self.calls)
            self.statuses.append(status)
            return status

        self.reporter = PipelineReporter(
            live_status_factory=factory,
            output=self.outputs.append,
        )

    def test_message_is_rendered(self) -> None:
        self.reporter.emit(PipelineEvent.message_event("hello"))

        self.assertEqual(self.outputs, ["hello"])
        self.assertEqual(self.calls, [])

    def test_single_stage_lifecycle_updates_and_finishes(self) -> None:
        self.reporter.emit(PipelineEvent.stage_start("refine"))
        self.reporter.emit(
            PipelineEvent.stage_progress(
                "refine",
                progress=0.5,
                detail="10/20 points",
                sub_detail="vehicle 2",
                elapsed=1.25,
                throughput="8 points/s",
            )
        )
        self.reporter.emit(
            PipelineEvent.stage_finish(
                "refine",
                progress=1.0,
                detail="20/20 points",
                elapsed=2.5,
            )
        )

        self.assertEqual(self.outputs, ["\n── refine ──"])
        self.assertEqual(
            self.calls,
            [
                ("start", "refine"),
                (
                    "update",
                    "refine",
                    {
                        "progress": 0.5,
                        "detail": "10/20 points",
                        "sub_detail": "vehicle 2",
                        "elapsed": 1.25,
                        "throughput": "8 points/s",
                        "stage": "refine",
                    },
                ),
                (
                    "update",
                    "refine",
                    {
                        "progress": 1.0,
                        "detail": "20/20 points",
                        "sub_detail": "",
                        "elapsed": 2.5,
                        "throughput": "",
                        "stage": "refine",
                    },
                ),
                ("stop", "refine"),
            ],
        )

    def test_stage_switch_stops_old_panel_before_starting_new_one(self) -> None:
        self.reporter.emit(PipelineEvent.stage_start("refine"))
        self.reporter.emit(PipelineEvent.stage_start("detect"))

        self.assertEqual(self.outputs, ["\n── refine ──", "\n── detect ──"])
        self.assertEqual(
            self.calls,
            [
                ("start", "refine"),
                ("stop", "refine"),
                ("start", "detect"),
            ],
        )

    def test_close_is_idempotent_and_stop_errors_propagate(self) -> None:
        self.reporter.emit(PipelineEvent.stage_start("refine"))
        self.reporter.close()
        self.reporter.close()
        self.assertEqual(self.calls, [("start", "refine"), ("stop", "refine")])

        class FailingStatus(FakeLiveStatus):
            def stop(self) -> None:
                raise RuntimeError("stop failed")

        failing = FailingStatus("broken", self.calls)
        failing_reporter = PipelineReporter(
            live_status_factory=lambda _stage: failing,
            output=self.outputs.append,
        )
        failing_reporter.emit(PipelineEvent.stage_start("broken"))
        with self.assertRaisesRegex(RuntimeError, "stop failed"):
            failing_reporter.close()

    def test_progress_without_stage_fails_fast(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "without an active stage"):
            self.reporter.emit(PipelineEvent.stage_progress("refine", progress=0.5))

    def test_finish_stops_panel_when_final_update_fails(self) -> None:
        class FailingUpdateStatus(FakeLiveStatus):
            def update(self, **kwargs: object) -> None:
                raise RuntimeError("update failed")

        status = FailingUpdateStatus("refine", self.calls)
        reporter = PipelineReporter(
            live_status_factory=lambda _stage: status,
            output=self.outputs.append,
        )
        reporter.emit(PipelineEvent.stage_start("refine"))

        with self.assertRaisesRegex(RuntimeError, "update failed"):
            reporter.emit(PipelineEvent.stage_finish("refine", progress=1.0))

        self.assertEqual(self.calls, [("start", "refine"), ("stop", "refine")])


if __name__ == "__main__":
    unittest.main()
