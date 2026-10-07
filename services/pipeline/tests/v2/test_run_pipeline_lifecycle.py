from __future__ import annotations

from .config_factory import (clip_config, fitting_config, intersect_config, network_config, pipeline_config, postgres_config, projection_config, refine_config, runtime_config)

import unittest
import numpy as np
from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from track.v2.config import PostgresConfig
from track.v2.config.pipeline_config_loader import PipelineConfig
from track.v2.db.pipeline_store import RefineBatchResult, DetectPersistenceResult
from track.v2.db.source_table import SourceTableContract
from track.v2.db.write_refine_results import RefineRunRecord
from track.v2.intersections.detect_intersections import IntersectConfig, IntersectResult
from track.v2.models import (
    RefineConfig,
    SourceBatch,
    ClipConfig,
    FittingConfig,
    IntersectReport,
    NetworkConfig,
    ProjectionConfig,
    RuntimeConfig,
)
from track.v2.orchestration.run_pipeline import PipelineInput, run_pipeline
from track.v2.orchestration.events import PipelineEvent, PipelineEventKind
from track.v2.projection import create_spatial_context


REFINE_ID = "11111111-1111-1111-1111-111111111111"
DETECT_ID = "22222222-2222-2222-2222-222222222222"
_PROJECTION_CONFIG = projection_config(processing_srid=3857)
_SPATIAL_CONTEXT = create_spatial_context(_PROJECTION_CONFIG)
_PIPELINE_CONFIG = replace(pipeline_config(), projection=_PROJECTION_CONFIG)


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
        self.info = SimpleNamespace(get_parameters=lambda: {"host": "postgres"})

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def cursor(self):
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        return cursor


class _Reader:
    instances: list["_Reader"] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs
        self.closed = False
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True


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
        self.clip_report = {
            "total_vehicles": 1,
            "total_subtracks": 2,
            "total_clips": 3,
            "between_intersection_clips": 4,
            "boundary_start_clips": 5,
            "boundary_end_clips": 6,
            "full_track_clips": 7,
            "total_points_in_clips": 8,
            "skipped_degenerate_count": 9,
        }
        self.clip_result.report.as_dict.return_value = self.clip_report
        self.fit_bundle = MagicMock()
        for variant in ("combined", "a_to_b", "b_to_a"):
            getattr(self.fit_bundle, variant).report.as_dict.return_value = {}
            getattr(self.fit_bundle, variant).issues = ()
        self.fit_report = {
            "input_clip_count": 10,
            "eligible_clip_count": 11,
            "connected_pair_count": 12,
            "fitted_centerline_count": 13,
            "skipped_clip_count": 14,
            "skipped_pair_count": 15,
        }
        self.fit_bundle.combined.report.as_dict.return_value = self.fit_report
        self.fit_bundle_error: RuntimeError | None = None
        self.clip_error: RuntimeError | None = None
        self.persist_detect_error: RuntimeError | None = None
        self.refine_status = "completed"
        self.completed_artifact: tuple[object, ...] | None = None
        self.completed_detect_artifact: tuple[object, ...] | None = None
        self.persist_detect_args: tuple[object, object] | None = None

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")

    def get_refine_run(self, refine_id: str) -> RefineRunRecord:
        self.events.append("get_refine")
        return replace(self.refine_record, status=self.refine_status)

    def start_refine_run(self, **kwargs: object) -> str:
        self.events.append("start_refine")
        self.refine_status = "running"
        return REFINE_ID

    def finish_refine(
        self,
        refine_id: str,
        *,
        status: str,
        source_fingerprint: dict[str, object] | None = None,
    ) -> None:
        self.events.append(f"finish_refine:{status}")
        self.refine_status = status

    def complete_refine_with_artifact(self, *args: object) -> None:
        self.events.extend(["artifact:metadata", "artifact:present", "finish_refine:completed"])
        self.completed_artifact = args
        self.refine_status = "completed"

    def persist_refine_batch(self, *args: object, **kwargs: object) -> RefineBatchResult:
        self.events.append("persist_refine_batch")
        return RefineBatchResult(next_subtrack_index=1, kept_count=0)

    def start_detect_run(self, **kwargs: object) -> str:
        self.events.append("start_detect")
        return next(self.detect_ids)

    def persist_detect(self, *args: object, **kwargs: object) -> DetectPersistenceResult:
        self.events.append("persist_detect")
        self.persist_detect_args = (kwargs["runtime"], kwargs["data_root"])
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

    def complete_detect_with_artifact(self, *args: object) -> None:
        self.events.extend(["artifact:metadata", "artifact:present", "finish_detect:completed"])
        self.completed_detect_artifact = args

    def load_clip(
        self, detect_id: str, *, publication_root: object,
        data_root: object, runtime: object, config: object, verify_sha256: bool,
    ) -> object:
        self.events.append("load_clip")
        self.loaded_clip_args = (
            publication_root, data_root, runtime, config, verify_sha256,
        )
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
        self.config = _PIPELINE_CONFIG
        self.events: list[str] = []
        _Reader.instances.clear()
        self.conn = _Connection(self.events)
        self.store = _RecordingStore(self.conn, self.events)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("track.v2.orchestration.run_pipeline.pg_connect", return_value=self.conn))
        self.stack.enter_context(
            patch("track.v2.orchestration.run_pipeline.PostgresPipelineStore", return_value=self.store)
        )
        self.stack.enter_context(patch("track.v2.orchestration.run_pipeline.require_current_schema"))
        source_contract = SourceTableContract(
            source_table="raw_points",
            quoted_table='"raw_points"',
            key_sql_type="bigint",
            has_extra=True,
        )
        self.stack.enter_context(patch(
            "track.v2.stages.refine.validate_source_table",
            return_value=source_contract,
        ))
        self.fingerprint_scan = self.stack.enter_context(patch(
            "track.v2.db.source_table.calculate_source_fingerprint",
            side_effect=AssertionError("Pipeline must not rescan source fingerprints"),
        ))
        self.refine_staging = SimpleNamespace(path="refine-staging", stage="refine")
        self.detect_staging = SimpleNamespace(path="detect-staging", stage="detect")
        self.descriptor = SimpleNamespace(id="artifact", relative_path=f"refine/{REFINE_ID}")
        self.detect_descriptor = SimpleNamespace(
            id="detect-artifact", relative_path=f"detect/{DETECT_ID}",
        )
        self.stack.enter_context(patch(
            "track.v2.stages.detect.find_refine_artifact",
            side_effect=lambda *_args: SimpleNamespace(
                descriptor=self.descriptor, state="present",
            ),
        ))
        self.stack.enter_context(patch("track.v2.stages.detect.RefineDatasetReader", _Reader))
        for module in ("stages.refine", "stages.detect"):
            self.stack.enter_context(patch(
                f"track.v2.{module}.create_staging",
                side_effect=lambda _root, stage, *_args: self.events.append(
                    f"create_staging:{stage}"
                ) or (self.refine_staging if stage == "refine" else self.detect_staging),
            ))
        self.export_refine = self.stack.enter_context(patch(
            "track.v2.stages.refine.export_refine_parquet",
            side_effect=lambda *args, **kwargs: self.events.append("export_refine") or self.descriptor,
        ))
        self.export_detect = self.stack.enter_context(patch(
            "track.v2.stages.detect.export_detect_results_to_parquet",
            side_effect=lambda *args, **kwargs: self.events.append("export_detect") or self.detect_descriptor,
        ))
        for module in ("stages.refine", "stages.detect"):
            self.stack.enter_context(patch(
                f"track.v2.{module}.publish",
                side_effect=lambda handle: self.events.append(f"publish:{handle.stage}"),
            ))
        self.abandon = MagicMock(
            side_effect=lambda handle: self.events.append(f"abandon:{handle.stage}"),
        )
        for module in ("stages.refine", "stages.detect"):
            self.stack.enter_context(patch(
                f"track.v2.{module}.abandon_staging", new=self.abandon,
            ))
        self.detect = self.stack.enter_context(
            patch("track.v2.stages.detect.detect_intersections", return_value=_empty_intersect_result())
        )
        self.handle = _EmptyHandle()
        self.stack.enter_context(
            patch("track.v2.stages.refine.get_database", return_value=self.handle)
        )
        self.stack.enter_context(patch("track.v2.stages.refine.StreamVehicleSource", _EmptyStream))
        self.stack.enter_context(
            patch("track.v2.stages.fitting.fit_centerline_variants", return_value=self.store.fit_bundle)
        )
        network_result = MagicMock()
        self.network_report = {
            "input_centerline_count": 16,
            "node_count": 17,
            "edge_count": 18,
            "isolated_node_count": 19,
            "skipped_centerline_count": 20,
        }
        network_result.report.as_dict.return_value = self.network_report
        self.stack.enter_context(patch(
            "track.v2.stages.network.build_network", return_value=network_result,
        ))
        self.log_info = MagicMock()
        for module in ("stages.support", "stages.detect", "stages.fitting"):
            self.stack.enter_context(patch(
                f"track.v2.{module}.log_info", new=self.log_info,
            ))

    def test_full_run_uses_stage_store_and_preserves_commit_order(self) -> None:
        emitted: list[PipelineEvent] = []
        result = run_pipeline(
            PipelineInput(source_table="raw_points"), self.config,
            emit=emitted.append,
        ).stats

        self.assertEqual((result["refine_id"], result["detect_id"]), (REFINE_ID, DETECT_ID))
        expected_keys = {
            "refine_id", "detect_id", "fit_id", "source_srid", "processing_srid",
            "source_points", "kept_trajectory_points", "injected_points",
            "detect_compute_s", "detect_persist_s", "resolved_sequence_s",
            "tracks_cross", "resolved_subtrack_points", "turn_events",
            "candidate_intersections", "candidate_entry_exit_points",
            "intersections", "entry_exit_points", "clip_assembly_s", "centerlines",
            "centerline_ids", "_elapsed_s",
            *(f"clip_{name}" for name in self.store.clip_report),
            *(f"fitting_{name}" for name in self.store.fit_report),
            *(f"network_{name}" for name in self.network_report),
        }
        self.assertEqual(set(result), expected_keys)
        for prefix, report in (
            ("clip", self.store.clip_report),
            ("fitting", self.store.fit_report),
            ("network", self.network_report),
        ):
            self.assertEqual(
                {f"{prefix}_{name}": value for name, value in report.items()},
                {
                    f"{prefix}_{name}": result[f"{prefix}_{name}"]
                    for name in report
                },
            )
        self.assertEqual(self.store.completed_artifact[0], REFINE_ID)
        self.assertIs(self.store.completed_artifact[1], self.descriptor)
        self.assertEqual(self.store.completed_detect_artifact[0], DETECT_ID)
        self.assertIs(self.store.completed_detect_artifact[1], self.detect_descriptor)
        self.assertEqual(
            self.store.loaded_clip_args,
            (
                _PIPELINE_CONFIG.paths.parquet_directory,
                _PIPELINE_CONFIG.paths.data_root,
                _PIPELINE_CONFIG.runtime,
                _PIPELINE_CONFIG.clip, _PIPELINE_CONFIG.resources.verify_artifact_sha256,
            ),
        )
        self.assertEqual(
            self.store.persist_detect_args,
            (_PIPELINE_CONFIG.runtime, _PIPELINE_CONFIG.paths.data_root),
        )
        self.assertEqual(self.descriptor.relative_path, f"refine/{REFINE_ID}")
        self.assertTrue(_Reader.instances[-1].closed)
        self.assertIs(self.detect.call_args.kwargs["refine_reader"], _Reader.instances[-1])
        self.assertEqual(
            self.events,
            [
                "start_refine", "commit", "persist_refine_batch",
                "commit", "create_staging:refine", "export_refine", "rollback",
                "publish:refine",
                "artifact:metadata", "artifact:present", "finish_refine:completed", "commit",
                "start_detect", "commit", "rollback",
                "persist_detect", "commit", "create_staging:detect",
                "export_detect", "rollback", "publish:detect",
                "artifact:metadata", "artifact:present", "finish_detect:completed", "commit",
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
            and event.detail in {"fit_compute", "fit_persist"}
        ]
        self.assertEqual(
            [event.detail for event in fitting_phases],
            ["fit_compute", "fit_persist"],
        )
        self.assertTrue(all(event.elapsed is not None for event in fitting_phases))
        heartbeat_details = [event.detail for event in emitted]
        self.assertTrue(any("结果行校验与事务提交" in detail for detail in heartbeat_details))
        self.assertTrue(any(
            "制品登记、完整性校验与事务提交" in detail
            for detail in heartbeat_details
        ))
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

    def test_detect_commit_failure_does_not_publish_completion(self) -> None:
        emitted = []
        original = self.store.commit

        def commit():
            if self.events[-1] == "finish_detect:completed":
                raise RuntimeError("validation failed")
            original()

        self.store.commit = commit
        with self.assertRaisesRegex(RuntimeError, "validation failed"):
            run_pipeline(PipelineInput(refine_id=REFINE_ID), self.config, emit=emitted.append)
        self.assertIn("finish_detect:failed", self.events)
        self.assertIn("publish:detect", self.events)
        self.assertNotIn("abandon:detect", self.events)
        self.assertFalse(any(event.kind == PipelineEventKind.STAGE_FINISH for event in emitted))
        self.assertFalse(any(event.progress == 1.0 for event in emitted))

    def test_detect_uses_run_identity_without_source_fingerprint_scans(self) -> None:
        run_pipeline(PipelineInput(refine_id=REFINE_ID), self.config)
        self.fingerprint_scan.assert_not_called()
        self.assertEqual(self.export_detect.call_args.args[4],
                         {"refine_id": REFINE_ID, "detect_id": DETECT_ID})
        self.assertIn("finish_detect:completed", self.events)

    def test_detect_export_failure_abandons_staging_and_marks_failed(self) -> None:
        self.export_detect.side_effect = RuntimeError("detect export failed")
        with self.assertRaisesRegex(RuntimeError, "detect export failed"):
            run_pipeline(PipelineInput(refine_id=REFINE_ID), self.config)

        self.assertIn("abandon:detect", self.events)
        self.assertNotIn("publish:detect", self.events)
        self.assertEqual(
            self.events[-4:],
            ["rollback", "abandon:detect", "finish_detect:failed", "commit"],
        )

    def test_detect_publish_failure_abandons_staging_and_marks_failed(self) -> None:
        def fail_detect_publish(handle: object) -> None:
            self.events.append(f"publish:{handle.stage}")
            if handle.stage == "detect":
                raise RuntimeError("detect publish failed")

        with (
            patch("track.v2.stages.detect.publish", side_effect=fail_detect_publish),
            self.assertRaisesRegex(RuntimeError, "detect publish failed"),
        ):
            run_pipeline(PipelineInput(refine_id=REFINE_ID), self.config)

        self.assertIn("abandon:detect", self.events)
        self.assertNotIn("artifact:metadata", self.events)
        self.assertEqual(
            self.events[-4:],
            ["rollback", "abandon:detect", "finish_detect:failed", "commit"],
        )

    def test_artifact_read_failure_closes_reader_and_marks_detect_failed(self) -> None:
        class _FailingReader(_Reader):
            def __enter__(self):
                super().__enter__()
                self.closed = True
                raise RuntimeError("artifact deleted")

        with (
            patch("track.v2.stages.detect.RefineDatasetReader", _FailingReader),
            self.assertRaisesRegex(RuntimeError, "artifact deleted"),
        ):
            run_pipeline(PipelineInput(refine_id=REFINE_ID), self.config)

        self.assertTrue(_FailingReader.instances[-1].closed)
        self.assertIn("finish_detect:failed", self.events)
        self.detect.assert_not_called()

    def test_missing_artifact_marks_detect_failed(self) -> None:
        with (
            patch("track.v2.stages.detect.find_refine_artifact", side_effect=RuntimeError("missing")),
            self.assertRaisesRegex(RuntimeError, "missing"),
        ):
            run_pipeline(PipelineInput(refine_id=REFINE_ID), self.config)
        self.assertIn("finish_detect:failed", self.events)
        self.detect.assert_not_called()

    def test_fingerprint_mismatch_closes_reader_and_marks_detect_failed(self) -> None:
        self.detect.side_effect = RuntimeError("fingerprint mismatch")
        with self.assertRaisesRegex(RuntimeError, "fingerprint mismatch"):
            run_pipeline(PipelineInput(refine_id=REFINE_ID), self.config)
        self.assertTrue(_Reader.instances[-1].closed)
        self.assertIn("finish_detect:failed", self.events)

    def test_fit_output_failure_does_not_rewrite_committed_run(self) -> None:
        def emit(event):
            if event.detail == "fit_persist":
                raise RuntimeError("output failed")

        with self.assertRaisesRegex(RuntimeError, "output failed"):
            run_pipeline(PipelineInput(refine_id=REFINE_ID), self.config, emit=emit)
        self.assertIn("finish_fit:completed", self.events)
        self.assertNotIn("finish_fit:failed", self.events)

    def test_detect_output_failure_does_not_rewrite_committed_run(self) -> None:
        def emit(event):
            if event.detail == "detect_persist":
                raise RuntimeError("detect output failed")

        with self.assertRaisesRegex(RuntimeError, "detect output failed"):
            run_pipeline(PipelineInput(refine_id=REFINE_ID), self.config, emit=emit)
        self.assertIn("finish_detect:completed", self.events)
        self.assertNotIn("finish_detect:failed", self.events)

    def test_completed_refine_reuses_store_record_without_refine_stage(self) -> None:
        first = run_pipeline(PipelineInput(refine_id=REFINE_ID), self.config).stats
        second = run_pipeline(PipelineInput(refine_id=REFINE_ID), self.config).stats

        self.assertEqual(
            (first["detect_id"], second["detect_id"]),
            (DETECT_ID, "33333333-3333-3333-3333-333333333333"),
        )
        self.assertEqual(self.events.count("start_refine"), 0)
        self.assertEqual(self.events.count("get_refine"), 2)
        self.assertNotIn("create_staging:refine", self.events)
        self.assertNotIn("publish:refine", self.events)
        for result in (first, second):
            self.assertTrue(
                {"source_points", "kept_trajectory_points", "injected_points"}.isdisjoint(result)
            )

    def test_refine_stream_failure_closes_source_and_marks_run_failed(self) -> None:
        class _FailingStream(_EmptyStream):
            def iter_vehicle_tracks(self):
                raise RuntimeError("stream failed")

        with (
            patch("track.v2.stages.refine.StreamVehicleSource", _FailingStream),
            self.assertRaisesRegex(RuntimeError, "stream failed"),
        ):
            run_pipeline(
                PipelineInput(source_table="raw_points"), self.config
            )

        self.assertEqual(
            self.events[-4:],
            ["rollback", "get_refine", "finish_refine:failed", "commit"],
        )
        self.assertEqual(self.handle.close_calls, 1)
        self.assertNotIn("start_detect", self.events)

    def test_refine_uses_run_identity_without_source_fingerprint_scans(self) -> None:
        run_pipeline(PipelineInput(source_table="raw_points"), self.config)
        self.fingerprint_scan.assert_not_called()
        self.assertEqual(self.export_refine.call_args.args[4], {"refine_id": REFINE_ID})
        self.assertIn("finish_refine:completed", self.events)

    def test_refine_export_failure_abandons_staging_and_preserves_error(self) -> None:
        with (
            patch(
                "track.v2.stages.refine.export_refine_parquet",
                side_effect=RuntimeError("export failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "export failed"),
        ):
            run_pipeline(PipelineInput(source_table="raw_points"), self.config)

        self.assertIn("abandon:refine", self.events)
        self.assertNotIn("publish:refine", self.events)
        self.assertIn("finish_refine:failed", self.events)

    def test_refine_registration_failure_keeps_published_orphan(self) -> None:
        def fail_completion(*args: object) -> None:
            self.events.append("artifact:metadata")
            raise RuntimeError("registry failed")

        self.store.complete_refine_with_artifact = fail_completion
        with self.assertRaisesRegex(RuntimeError, "registry failed"):
            run_pipeline(PipelineInput(source_table="raw_points"), self.config)

        self.assertIn("publish:refine", self.events)
        self.assertNotIn("abandon:refine", self.events)
        self.assertEqual(self.events[-3:], ["get_refine", "finish_refine:failed", "commit"])

    def test_projection_bounds_failure_closes_source_and_marks_run_failed(self) -> None:
        batch = SourceBatch("source", "v", np.array([[116.,39.]]), np.array([0.]),
                            np.array([0.]), np.array([10.]), np.array([1],dtype=np.int64),
                            np.array([True]), np.array([""],dtype=object))

        class _OneTrackStream(_EmptyStream):
            def iter_vehicle_tracks(self):
                return iter((batch,))

        transformer = MagicMock()
        transformer.validate_bounds.side_effect = ValueError("outside projection bounds")
        refine_result = SimpleNamespace(
            derived_counts={},
            excluded=[],
            subtracks=[],
        )
        with (
            patch("track.v2.stages.refine.StreamVehicleSource", _OneTrackStream),
            patch("track.v2.stages.refine.CoordinateTransformer", return_value=transformer),
            patch("track.v2.stages.refine.refine_vehicle", return_value=refine_result),
            self.assertRaisesRegex(ValueError, "outside projection bounds"),
        ):
            run_pipeline(
                PipelineInput(source_table="raw_points"), self.config
            )

        transformer.validate_bounds.assert_called_once_with((116.0, 39.0, 116.0, 39.0))
        self.assertEqual(
            self.events[-4:],
            ["rollback", "get_refine", "finish_refine:failed", "commit"],
        )
        self.assertEqual(self.handle.close_calls, 1)
        self.assertNotIn("persist_refine_batch", self.events)
        self.assertNotIn("start_detect", self.events)

    def test_clip_failure_does_not_undo_completed_detect(self) -> None:
        self.store.clip_error = RuntimeError("clip failed")

        with self.assertRaisesRegex(RuntimeError, "clip failed"):
            run_pipeline(PipelineInput(refine_id=REFINE_ID), self.config)

        self.assertEqual(self.events[-6:], [
            "publish:detect", "artifact:metadata", "artifact:present",
            "finish_detect:completed", "commit", "load_clip",
        ])
        self.assertEqual(self.events.count("rollback"), 2)

    def test_clip_report_cancellation_closes_loaded_resource_once(self) -> None:
        def emit(event: PipelineEvent) -> None:
            if event.message == "  Clip 报告 / Clip report":
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            run_pipeline(PipelineInput(refine_id=REFINE_ID), self.config, emit=emit)

        self.store.clip_result.clips.close.assert_called_once_with()

    def test_detect_persistence_failure_rolls_back_and_marks_detect_failed(self) -> None:
        self.store.persist_detect_error = RuntimeError("detect write failed")

        with self.assertRaisesRegex(RuntimeError, "detect write failed"):
            run_pipeline(PipelineInput(refine_id=REFINE_ID), self.config)

        self.assertEqual(self.events[-5:], [
            "rollback", "persist_detect", "rollback", "finish_detect:failed", "commit",
        ])
        self.assertNotIn("finish_detect:completed", self.events)

    def test_detect_compute_failure_closes_snapshot_and_marks_detect_failed(self) -> None:
        with (
            patch(
                "track.v2.stages.detect.detect_intersections",
                side_effect=RuntimeError("detect compute failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "detect compute failed"),
        ):
            run_pipeline(PipelineInput(refine_id=REFINE_ID), self.config)

        self.assertEqual(
            self.events[-4:],
            ["commit", "rollback", "finish_detect:failed", "commit"],
        )
        self.assertNotIn("persist_detect", self.events)

    def test_fit_expected_failure_rolls_back_and_marks_fit_failed(self) -> None:
        self.store.fit_bundle_error = RuntimeError("write failed")

        with self.assertRaisesRegex(RuntimeError, "write failed"):
            run_pipeline(PipelineInput(refine_id=REFINE_ID), self.config)

        self.assertEqual(self.events[-5:], [
            "commit", "persist_fit", "rollback", "finish_fit:failed", "commit",
        ])
        self.store.clip_result.clips.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
