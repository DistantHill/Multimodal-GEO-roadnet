from __future__ import annotations

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from track.v2.config import PostgresConfig
from track.v2.config.pipeline_config import PipelineConfig
from track.v2.db.pipeline_store import RefineBatchResult, DetectPersistenceResult
from track.v2.db.source_table import SourceTableContract
from track.v2.db_writer import RefineRunRecord
from track.v2.detect import IntersectConfig, IntersectResult
from track.v2.models import (
    RefineConfig,
    ClipConfig,
    FittingConfig,
    IntersectReport,
    NetworkConfig,
    ProjectionConfig,
    RuntimeConfig,
)
from track.v2.pipeline import PipelineInput, run_pipeline
from track.v2.pipeline_events import PipelineEvent, PipelineEventKind
from track.v2.projection import create_spatial_context


REFINE_ID = "11111111-1111-1111-1111-111111111111"
DETECT_ID = "22222222-2222-2222-2222-222222222222"
_PROJECTION_CONFIG = ProjectionConfig(processing_srid=3857)
_SPATIAL_CONTEXT = create_spatial_context(_PROJECTION_CONFIG)
_PIPELINE_CONFIG = PipelineConfig(
    projection=_PROJECTION_CONFIG,
    refine=RefineConfig(),
    detect=IntersectConfig(),
    clip=ClipConfig(),
    fitting=FittingConfig(),
    network=NetworkConfig(),
    runtime=RuntimeConfig(),
)


def _empty_intersect_result() -> IntersectResult:
    return IntersectResult(
        turn_events=[],
        hotspot_events=[],
        clusters=[],
        topologies=[],
        observations=[],
        cross_track_candidates=[],
        report=IntersectReport(
            track_count=0,
            turn_event_count=0,
            hotspot_event_count=0,
            cross_track_candidate_count=0,
            cluster_count=0,
            cluster_count_turn=0,
            cluster_count_cross_track=0,
            complete_intersection_count=0,
        ),
        spatial_context=_SPATIAL_CONTEXT,
    )


class _Connection:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


class _EmptyHandle:
    def __init__(self) -> None:
        self.close_calls = 0

    def count_rows(self) -> int:
        return 0

    def close(self) -> None:
        self.close_calls += 1


class _EmptyStream:
    def __init__(self, handle: object) -> None:
        self.handle = handle

    def iter_vehicle_tracks(self):
        return iter(())

    def close(self) -> None:
        self.handle.close()


class _RecordingStore:
    def __init__(self, conn: _Connection, events: list[str]) -> None:
        self.connection = conn
        self.events = events
        self.detect_ids = iter((DETECT_ID, "33333333-3333-3333-3333-333333333333"))
        self.refine_record = RefineRunRecord(
            id=REFINE_ID,
            source_table="raw_points",
            parameters={},
            status="completed",
            spatial_context=_SPATIAL_CONTEXT,
            source_fingerprint={"version": 1, "row_count": 0, "sha256": "0"},
        )
        self.clip_result = MagicMock()
        self.clip_result.report.as_dict.return_value = {}
        self.fit_bundle = MagicMock()
        for variant in ("combined", "a_to_b", "b_to_a"):
            getattr(self.fit_bundle, variant).report.as_dict.return_value = {}
            getattr(self.fit_bundle, variant).issues = ()
        self.fit_bundle_error: RuntimeError | None = None
        self.clip_error: RuntimeError | None = None
        self.persist_detect_error: RuntimeError | None = None

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")

    def get_refine_run(self, refine_id: str) -> RefineRunRecord:
        self.events.append("get_refine")
        return self.refine_record

    def start_refine_run(self, **kwargs: object) -> str:
        self.events.append("start_refine")
        return REFINE_ID

    def finish_refine(
        self,
        refine_id: str,
        *,
        status: str,
        source_fingerprint: dict[str, object] | None = None,
    ) -> None:
        self.events.append(f"finish_refine:{status}")

    def persist_refine_batch(self, *args: object, **kwargs: object) -> RefineBatchResult:
        self.events.append("persist_refine_batch")
        return RefineBatchResult(next_subtrack_index=1, kept_count=0)

    def start_detect_run(self, **kwargs: object) -> str:
        self.events.append("start_detect")
        return next(self.detect_ids)

    def persist_detect(self, *args: object, **kwargs: object) -> DetectPersistenceResult:
        self.events.append("persist_detect")
        if self.persist_detect_error is not None:
            raise self.persist_detect_error
        return DetectPersistenceResult(
            resolved_point_count=0,
            tracks_cross_count=0,
            turn_event_count=0,
            candidate_intersection_count=0,
            candidate_entry_exit_point_count=0,
            intersection_count=0,
            entry_exit_point_count=0,
        )

    def finish_detect(self, detect_id: str, *, status: str) -> None:
        self.events.append(f"finish_detect:{status}")

    def load_clip(self, detect_id: str, *, config: object) -> object:
        self.events.append("load_clip")
        if self.clip_error is not None:
            raise self.clip_error
        return self.clip_result

    def start_fit_run(self, **kwargs: object) -> str:
        self.events.append("start_fit")
        return "fit-1"

    def persist_fit_bundle(self, *args: object, **kwargs: object) -> tuple[int, ...]:
        self.events.append("persist_fit")
        if self.fit_bundle_error is not None:
            raise self.fit_bundle_error
        return ()

    def finish_fit(self, fit_id: str, *, status: str, report: object = None) -> None:
        self.events.append(f"finish_fit:{status}")


class RunPipelineLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = PostgresConfig(
            host="localhost", port=5432, database="track", user="track", password="",
        )
        self.events: list[str] = []
        self.conn = _Connection(self.events)
        self.store = _RecordingStore(self.conn, self.events)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("track.v2.pipeline.pg_connect", return_value=self.conn))
        self.stack.enter_context(
            patch("track.v2.pipeline.load_pipeline_config", return_value=_PIPELINE_CONFIG)
        )
        self.stack.enter_context(
            patch("track.v2.pipeline.PostgresPipelineStore", return_value=self.store)
        )
        self.stack.enter_context(patch("track.v2.pipeline.require_schema_v4"))
        self.stack.enter_context(patch("track.v2.pipeline.verify_refine_source_fingerprint"))
        self.stack.enter_context(
            patch(
                "track.v2.pipeline.validate_source_table",
                return_value=SourceTableContract(
                    source_table="raw_points",
                    quoted_table='"raw_points"',
                    key_sql_type="bigint",
                    has_extra=True,
                ),
            )
        )
        self.stack.enter_context(
            patch(
                "track.v2.pipeline.calculate_source_fingerprint",
                return_value={"version": 1, "row_count": 0, "sha256": "0"},
            )
        )
        self.stack.enter_context(
            patch("track.v2.pipeline.detect_intersections", return_value=_empty_intersect_result())
        )
        self.handle = _EmptyHandle()
        self.stack.enter_context(
            patch("track.v2.pipeline.get_database", return_value=self.handle)
        )
        self.stack.enter_context(patch("track.v2.pipeline.StreamVehicleSource", _EmptyStream))
        self.stack.enter_context(
            patch("track.v2.pipeline.fit_centerline_variants", return_value=self.store.fit_bundle)
        )
        self.stack.enter_context(patch("track.v2.pipeline.build_network", return_value=MagicMock()))
        self.log_info = self.stack.enter_context(patch("track.v2.pipeline.log_info"))
        self.stack.enter_context(patch("track.v2.pipeline._add_clip_stats"))
        self.stack.enter_context(patch("track.v2.pipeline._add_fit_stats"))
        self.stack.enter_context(patch("track.v2.pipeline._add_network_stats"))

    def test_full_run_uses_stage_store_and_preserves_commit_order(self) -> None:
        emitted: list[PipelineEvent] = []
        result = run_pipeline(
            PipelineInput(self.config, source_table="raw_points"),
            emit=emitted.append,
        ).stats

        self.assertEqual((result["refine_id"], result["detect_id"]), (REFINE_ID, DETECT_ID))
        self.assertEqual(
            self.events,
            [
                "start_refine", "commit", "persist_refine_batch", "finish_refine:completed", "commit",
                "start_detect", "commit", "rollback",
                "persist_detect", "finish_detect:completed", "commit",
                "load_clip", "start_fit", "commit", "persist_fit", "finish_fit:completed", "commit",
            ],
        )
        self.assertEqual(
            [event.kind for event in emitted if event.kind in {
                PipelineEventKind.STAGE_START, PipelineEventKind.STAGE_FINISH,
            }],
            [
                PipelineEventKind.STAGE_START,
                PipelineEventKind.STAGE_FINISH,
                PipelineEventKind.STAGE_START,
                PipelineEventKind.STAGE_FINISH,
                PipelineEventKind.STAGE_START,
                PipelineEventKind.STAGE_FINISH,
                PipelineEventKind.STAGE_START,
                PipelineEventKind.STAGE_FINISH,
                PipelineEventKind.STAGE_START,
                PipelineEventKind.STAGE_FINISH,
            ],
        )
        log_messages = [call.args[0] for call in self.log_info.call_args_list]
        self.assertEqual(
            [message for message in log_messages if message.startswith("[stage-start]")],
            [
                f"[stage-start] stage=refine refine_id={REFINE_ID}",
                f"[stage-start] stage=detect refine_id={REFINE_ID} detect_id={DETECT_ID}",
                f"[stage-start] stage=clip refine_id={REFINE_ID} detect_id={DETECT_ID}",
                f"[stage-start] stage=fitting refine_id={REFINE_ID} detect_id={DETECT_ID} fit_id=fit-1",
                f"[stage-start] stage=network refine_id={REFINE_ID} detect_id={DETECT_ID} fit_id=fit-1",
            ],
        )
        finish_messages = [
            message for message in log_messages if message.startswith("[stage-finish]")
        ]
        self.assertEqual(len(finish_messages), 5)
        for stage, message in zip(
            ("refine", "detect", "clip", "fitting", "network"),
            finish_messages,
            strict=True,
        ):
            self.assertIn(f"stage={stage}", message)
            self.assertIn("status=completed", message)
            self.assertRegex(message, r"elapsed_s=\d+\.\d{3}$")

        finish_events = [
            event for event in emitted if event.kind == PipelineEventKind.STAGE_FINISH
        ]
        self.assertEqual(len(finish_events), 5)
        self.assertTrue(all(event.elapsed is not None for event in finish_events))

        fitting_phases = [
            event for event in emitted
            if event.kind == PipelineEventKind.STAGE_PROGRESS
            and event.stage == "Fitting: 中心线拟合 / Centerline fitting"
        ]
        self.assertEqual(
            [event.detail for event in fitting_phases],
            ["fit_compute", "fit_persist"],
        )
        self.assertTrue(all(event.elapsed is not None for event in fitting_phases))
        phase_messages = [
            message for message in log_messages if message.startswith("[stage-phase]")
        ]
        self.assertEqual(len(phase_messages), 4)
        for phase, message in zip(
            ("detect_compute", "detect_persist", "fit_compute", "fit_persist"),
            phase_messages,
            strict=True,
        ):
            stage = "detect" if phase.startswith("detect_") else "fitting"
            self.assertIn(f"stage={stage} phase={phase}", message)
            self.assertRegex(message, r"elapsed_s=\d+\.\d{3}")

    def test_completed_refine_reuses_store_record_without_refine_stage(self) -> None:
        first = run_pipeline(PipelineInput(self.config, refine_id=REFINE_ID)).stats
        second = run_pipeline(PipelineInput(self.config, refine_id=REFINE_ID)).stats

        self.assertEqual(
            (first["detect_id"], second["detect_id"]),
            (DETECT_ID, "33333333-3333-3333-3333-333333333333"),
        )
        self.assertEqual(self.events.count("start_refine"), 0)
        self.assertEqual(self.events.count("get_refine"), 2)

    def test_refine_stream_failure_closes_source_and_marks_run_failed(self) -> None:
        class _FailingStream(_EmptyStream):
            def iter_vehicle_tracks(self):
                raise RuntimeError("stream failed")

        with (
            patch("track.v2.pipeline.StreamVehicleSource", _FailingStream),
            self.assertRaisesRegex(RuntimeError, "stream failed"),
        ):
            run_pipeline(
                PipelineInput(
                    self.config,
                    source_table="raw_points",
                )
            )

        self.assertEqual(
            self.events[-3:],
            ["rollback", "finish_refine:failed", "commit"],
        )
        self.assertEqual(self.handle.close_calls, 1)
        self.assertNotIn("start_detect", self.events)

    def test_projection_bounds_failure_closes_source_and_marks_run_failed(self) -> None:
        point = SimpleNamespace(lon=116.0, lat=39.0)

        class _OneTrackStream(_EmptyStream):
            def iter_vehicle_tracks(self):
                return iter(([point],))

        transformer = MagicMock()
        transformer.validate_bounds.side_effect = ValueError("outside projection bounds")
        refine_result = SimpleNamespace(
            derived_counts={},
            excluded=[],
            subtracks=[],
        )
        with (
            patch("track.v2.pipeline.StreamVehicleSource", _OneTrackStream),
            patch("track.v2.pipeline.CoordinateTransformer", return_value=transformer),
            patch("track.v2.pipeline.refine_vehicle", return_value=refine_result),
            self.assertRaisesRegex(ValueError, "outside projection bounds"),
        ):
            run_pipeline(
                PipelineInput(
                    self.config,
                    source_table="raw_points",
                )
            )

        transformer.validate_bounds.assert_called_once_with((116.0, 39.0, 116.0, 39.0))
        self.assertEqual(
            self.events[-3:],
            ["rollback", "finish_refine:failed", "commit"],
        )
        self.assertEqual(self.handle.close_calls, 1)
        self.assertNotIn("persist_refine_batch", self.events)
        self.assertNotIn("start_detect", self.events)

    def test_clip_failure_does_not_undo_completed_detect(self) -> None:
        self.store.clip_error = RuntimeError("clip failed")

        with self.assertRaisesRegex(RuntimeError, "clip failed"):
            run_pipeline(PipelineInput(self.config, refine_id=REFINE_ID))

        self.assertEqual(self.events[-5:], [
            "rollback", "persist_detect", "finish_detect:completed", "commit", "load_clip",
        ])
        self.assertEqual(self.events.count("rollback"), 1)

    def test_detect_persistence_failure_rolls_back_and_marks_detect_failed(self) -> None:
        self.store.persist_detect_error = RuntimeError("detect write failed")

        with self.assertRaisesRegex(RuntimeError, "detect write failed"):
            run_pipeline(PipelineInput(self.config, refine_id=REFINE_ID))

        self.assertEqual(self.events[-5:], [
            "rollback", "persist_detect", "rollback", "finish_detect:failed", "commit",
        ])
        self.assertNotIn("finish_detect:completed", self.events)

    def test_detect_compute_failure_closes_snapshot_and_marks_detect_failed(self) -> None:
        with (
            patch(
                "track.v2.pipeline.detect_intersections",
                side_effect=RuntimeError("detect compute failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "detect compute failed"),
        ):
            run_pipeline(PipelineInput(self.config, refine_id=REFINE_ID))

        self.assertEqual(
            self.events[-4:],
            ["commit", "rollback", "finish_detect:failed", "commit"],
        )
        self.assertNotIn("persist_detect", self.events)

    def test_fit_expected_failure_rolls_back_and_marks_fit_failed(self) -> None:
        self.store.fit_bundle_error = RuntimeError("write failed")

        with self.assertRaisesRegex(RuntimeError, "write failed"):
            run_pipeline(PipelineInput(self.config, refine_id=REFINE_ID))

        self.assertEqual(self.events[-5:], [
            "commit", "persist_fit", "rollback", "finish_fit:failed", "commit",
        ])


if __name__ == "__main__":
    unittest.main()
