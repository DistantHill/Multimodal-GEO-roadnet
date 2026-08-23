from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from track.v2.pipeline import PipelineInput, _flush_clean_batch, run_pipeline
from track.v2.config import PostgresConfig
from track.v2.db_writer import CleanRunRecord
from track.v2.intersect2 import IntersectResult
from track.v2.models import (
    CenterlineFitBundle,
    DerivedPoint,
    GeneratedReason,
    IntersectReport,
    PointRef,
    RefinementSubtrack,
    TrajectoryPoint,
)


CLEAN_ID = "11111111-1111-1111-1111-111111111111"
DETECT_ID = "22222222-2222-2222-2222-222222222222"


def _empty_intersect_result() -> IntersectResult:
    return IntersectResult(
        turn_events=[],
        hotspot_events=[],
        orthogonal_candidates=[],
        clusters=[],
        topologies=[],
        report=IntersectReport(
            track_count=0,
            turn_event_count=0,
            hotspot_event_count=0,
            orthogonal_candidate_count=0,
            cross_track_candidate_count=0,
            cluster_count=0,
            cluster_count_turn=0,
            cluster_count_ortho=0,
            cluster_count_cross_track=0,
            complete_intersection_count=0,
        ),
    )


class _Connection:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")


class _EmptyHandle:
    def count_rows(self) -> int:
        return 0

    def close(self) -> None:
        return None


class _EmptyStream:
    def __init__(self, handle: object) -> None:
        self.handle = handle

    def iter_vehicle_tracks(self):
        return iter(())

    def close(self) -> None:
        return None


def _point(
    row_key: str,
    *,
    vehicle_id: str = "vehicle-1",
    generated: bool = False,
) -> TrajectoryPoint:
    point_type = DerivedPoint if generated else TrajectoryPoint
    return point_type(
        point_ref=PointRef(
            source_id="raw_points",
            source_row_key=row_key,
            stable=not generated,
            from_ref="1" if generated else None,
            to_ref="2" if generated else None,
            generated_reason=(
                GeneratedReason.HEADING_ORTHOGONAL if generated else ""
            ),
        ),
        vehicle_id=vehicle_id,
        timestamp=float(row_key),
        lon=116.0,
        lat=39.0,
        heading=0.0,
        speed=10.0,
    )


class FlushCleanBatchTest(unittest.TestCase):
    def test_assigns_one_global_subtrack_index_for_both_writers(self) -> None:
        pending_subtracks = [
            RefinementSubtrack(
                vehicle_id="vehicle-1",
                subtrack_index=1,
                points=[_point("1"), _point("15", generated=True), _point("2")],
            ),
            RefinementSubtrack(
                vehicle_id="vehicle-2",
                subtrack_index=1,
                points=[_point("3", vehicle_id="vehicle-2")],
            ),
        ]

        with (
            patch("track.v2.pipeline.insert_kept_trajectory_points") as insert_kept,
            patch("track.v2.pipeline.insert_injected_points") as insert_injected,
        ):
            next_index, kept_count = _flush_clean_batch(
                object(),
                clean_id=CLEAN_ID,
                table="raw_points",
                tp_id_map={},
                pending_excluded=[],
                pending_subtracks=pending_subtracks,
                pending_tp_keys=[],
                subtrack_index=10,
                batch_size=5000,
            )

        kept_subtracks = insert_kept.call_args.args[1]
        injected_subtracks = insert_injected.call_args.args[1]
        self.assertIs(kept_subtracks, injected_subtracks)
        self.assertEqual([subtrack.subtrack_index for subtrack in kept_subtracks], [10, 11])
        self.assertEqual((next_index, kept_count), (12, 3))
        self.assertEqual(pending_subtracks, [])


class RunPipelineLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = PostgresConfig(
            host="localhost", port=5432, database="track", user="track", password="",
        )
        self.events: list[str] = []
        self.conn = _Connection(self.events)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("track.v2.pipeline.pg_connect", return_value=self.conn))
        self.stack.enter_context(patch("track.v2.pipeline.LiveStatus", return_value=MagicMock()))
        self.stack.enter_context(
            patch("track.v2.pipeline.detect_intersections", return_value=_empty_intersect_result())
        )
        self.stack.enter_context(patch("track.v2.pipeline.insert_tracks_cross", return_value=0))
        self.stack.enter_context(patch("track.v2.pipeline.insert_turn_events", return_value={}))
        self.stack.enter_context(
            patch("track.v2.pipeline.insert_candidate_intersections", return_value={})
        )
        self.stack.enter_context(patch("track.v2.pipeline.insert_candidate_intersection_events"))
        self.stack.enter_context(
            patch("track.v2.pipeline.insert_candidate_entry_exit_points", return_value={})
        )
        self.stack.enter_context(patch("track.v2.pipeline.insert_intersections", return_value={}))
        self.stack.enter_context(patch("track.v2.pipeline.insert_entry_exit_points"))
        self.stack.enter_context(patch("track.v2.pipeline._add_clip_stats"))
        self.stack.enter_context(patch("track.v2.pipeline._print_clip_report"))
        self.stack.enter_context(patch("track.v2.pipeline._add_fit_stats"))
        self.stack.enter_context(patch("track.v2.pipeline._add_network_stats"))
        empty_fit = MagicMock()
        empty_fit.centerlines = ()
        self.stack.enter_context(
            patch(
                "track.v2.pipeline.fit_centerline_variants",
                return_value=CenterlineFitBundle(empty_fit, empty_fit, empty_fit),
            )
        )
        self.stack.enter_context(
            patch("track.v2.pipeline.create_fit_run", return_value="fit-1")
        )
        self.stack.enter_context(
            patch("track.v2.pipeline.insert_centerline_bundle", return_value=())
        )
        self.stack.enter_context(patch("track.v2.pipeline.finish_fit_run"))
        self.stack.enter_context(patch("track.v2.pipeline.build_network", return_value=MagicMock()))

    def test_full_run_commits_clean_then_detect_before_post_processing(self) -> None:
        self.stack.enter_context(patch("track.v2.pipeline.get_database", return_value=_EmptyHandle()))
        self.stack.enter_context(patch("track.v2.pipeline.StreamVehicleSource", _EmptyStream))
        create_clean = self.stack.enter_context(
            patch(
                "track.v2.pipeline.create_clean_run",
                side_effect=lambda *args, **kwargs: self.events.append("create_clean") or CLEAN_ID,
            )
        )
        finish_clean = self.stack.enter_context(
            patch(
                "track.v2.pipeline.finish_clean_run",
                side_effect=lambda *args, **kwargs: self.events.append("finish_clean"),
            )
        )
        create_detect = self.stack.enter_context(
            patch(
                "track.v2.pipeline.create_detect_run",
                side_effect=lambda *args, **kwargs: self.events.append("create_detect") or DETECT_ID,
            )
        )
        finish_detect = self.stack.enter_context(
            patch(
                "track.v2.pipeline.finish_detect_run",
                side_effect=lambda *args, **kwargs: self.events.append("finish_detect"),
            )
        )
        self.stack.enter_context(
            patch(
                "track.v2.pipeline.extract_clips_for_detect",
                side_effect=lambda *args, **kwargs: self.events.append("clip") or MagicMock(),
            )
        )

        stats = run_pipeline(
            PipelineInput(self.config, pg_source_table="raw_points", table="raw_points")
        ).stats

        self.assertEqual(stats["clean_id"], CLEAN_ID)
        self.assertEqual(stats["detect_id"], DETECT_ID)
        self.assertNotIn("run_id", stats)
        self.assertEqual(
            self.events,
            [
                "create_clean", "commit", "finish_clean", "commit",
                "create_detect", "commit", "finish_detect", "commit", "clip",
                "commit", "commit",
            ],
        )
        create_clean.assert_called_once()
        finish_clean.assert_called_once_with(self.conn, CLEAN_ID, status="completed")
        create_detect.assert_called_once_with(
            self.conn, clean_id=CLEAN_ID, parameters={"target_table": "raw_points"},
        )
        finish_detect.assert_called_once_with(self.conn, DETECT_ID, status="completed")

    def test_completed_clean_can_create_multiple_detect_runs_without_cleaning_again(self) -> None:
        fetch_clean = self.stack.enter_context(
            patch(
                "track.v2.pipeline.fetch_clean_run",
                return_value=CleanRunRecord(
                    id=CLEAN_ID,
                    source_id="raw_points",
                    source_kind="postgres_table",
                    parameters={"target_table": "raw_points", "cleaner_max_gap_s": 60.0},
                    status="completed",
                ),
            )
        )
        create_clean = self.stack.enter_context(patch("track.v2.pipeline.create_clean_run"))
        self.stack.enter_context(patch("track.v2.pipeline.finish_clean_run"))
        create_detect = self.stack.enter_context(
            patch(
                "track.v2.pipeline.create_detect_run",
                side_effect=(DETECT_ID, "33333333-3333-3333-3333-333333333333"),
            )
        )
        self.stack.enter_context(patch("track.v2.pipeline.finish_detect_run"))
        self.stack.enter_context(
            patch("track.v2.pipeline.extract_clips_for_detect", return_value=MagicMock())
        )

        first = run_pipeline(PipelineInput(self.config, clean_id=CLEAN_ID)).stats
        second = run_pipeline(PipelineInput(self.config, clean_id=CLEAN_ID)).stats

        self.assertEqual((first["detect_id"], second["detect_id"]), (
            DETECT_ID, "33333333-3333-3333-3333-333333333333",
        ))
        create_clean.assert_not_called()
        self.assertEqual(fetch_clean.call_count, 2)
        self.assertEqual(create_detect.call_count, 2)
        for call in create_detect.call_args_list:
            self.assertEqual(call.kwargs["clean_id"], CLEAN_ID)

    def test_clip_failure_does_not_undo_completed_detect(self) -> None:
        self.stack.enter_context(
            patch(
                "track.v2.pipeline.fetch_clean_run",
                return_value=CleanRunRecord(
                    id=CLEAN_ID,
                    source_id="raw_points",
                    source_kind="postgres_table",
                    parameters={"target_table": "raw_points"},
                    status="completed",
                ),
            )
        )
        self.stack.enter_context(patch("track.v2.pipeline.create_detect_run", return_value=DETECT_ID))
        finish_detect = self.stack.enter_context(
            patch(
                "track.v2.pipeline.finish_detect_run",
                side_effect=lambda *args, **kwargs: self.events.append("finish_detect"),
            )
        )
        self.stack.enter_context(
            patch(
                "track.v2.pipeline.extract_clips_for_detect",
                side_effect=lambda *args, **kwargs: (
                    self.events.append("clip"), (_ for _ in ()).throw(RuntimeError("clip failed"))
                )[1],
            )
        )

        with self.assertRaisesRegex(RuntimeError, "clip failed"):
            run_pipeline(PipelineInput(self.config, clean_id=CLEAN_ID))

        finish_detect.assert_called_once_with(self.conn, DETECT_ID, status="completed")
        self.assertEqual(self.events[-3:], ["finish_detect", "commit", "clip"])

    def test_fit_write_failure_rolls_back_outputs_and_marks_fit_failed(self) -> None:
        self.stack.enter_context(
            patch(
                "track.v2.pipeline.fetch_clean_run",
                return_value=CleanRunRecord(
                    id=CLEAN_ID, source_id="raw_points", source_kind="postgres_table",
                    parameters={"target_table": "raw_points"}, status="completed",
                ),
            )
        )
        self.stack.enter_context(patch("track.v2.pipeline.create_detect_run", return_value=DETECT_ID))
        self.stack.enter_context(patch("track.v2.pipeline.finish_detect_run"))
        self.stack.enter_context(
            patch("track.v2.pipeline.extract_clips_for_detect", return_value=MagicMock())
        )
        finish_fit = self.stack.enter_context(patch("track.v2.pipeline.finish_fit_run"))
        self.stack.enter_context(
            patch("track.v2.pipeline.insert_centerline_bundle", side_effect=RuntimeError("write failed"))
        )

        with self.assertRaisesRegex(RuntimeError, "write failed"):
            run_pipeline(PipelineInput(self.config, clean_id=CLEAN_ID))

        self.assertIn("rollback", self.events)
        finish_fit.assert_called_once_with(self.conn, "fit-1", status="failed")


if __name__ == "__main__":
    unittest.main()
