"""Stage-level persistence seam tests."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from track.v2.db.pipeline_store import (
    CleanBatchResult,
    DetectPersistenceResult,
    PostgresPipelineStore,
    _candidate_memberships,
)
from track.v2.models import (
    ClipConfig,
    DerivedPoint,
    FittingConfig,
    GeneratedReason,
    PointRef,
    RefinementSubtrack,
    TrajectoryPoint,
)


class _Connection:
    def __init__(self) -> None:
        self.events: list[str] = []

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")


def _point(row_key: str, *, generated: bool = False) -> TrajectoryPoint:
    point_type = DerivedPoint if generated else TrajectoryPoint
    return point_type(
        point_ref=PointRef(
            source_id="source",
            source_row_key=row_key,
            stable=not generated,
            generated_reason=(
                GeneratedReason.HEADING_ORTHOGONAL if generated else ""
            ),
        ),
        vehicle_id="vehicle-1",
        timestamp=float(row_key),
        lon=116.0,
        lat=39.0,
        heading=0.0,
        speed=10.0,
    )


class PipelineStoreTransactionTest(unittest.TestCase):
    def test_connection_and_transaction_methods_are_transparent(self) -> None:
        conn = _Connection()
        store = PostgresPipelineStore(conn)

        self.assertIs(store.connection, conn)
        store.commit()
        store.rollback()

        self.assertEqual(conn.events, ["commit", "rollback"])

    def test_transaction_errors_propagate(self) -> None:
        conn = MagicMock()
        conn.commit.side_effect = RuntimeError("commit failed")
        store = PostgresPipelineStore(conn)

        with self.assertRaisesRegex(RuntimeError, "commit failed"):
            store.commit()


class PipelineStoreCleanTest(unittest.TestCase):
    def test_clean_lifecycle_and_raw_write_delegate(self) -> None:
        conn = object()
        store = PostgresPipelineStore(conn)
        clean_record = object()
        with (
            patch("track.v2.db.pipeline_store.fetch_clean_run", return_value=clean_record) as fetch,
            patch("track.v2.db.pipeline_store.create_clean_run", return_value="clean-1") as start,
            patch("track.v2.db.pipeline_store.finish_clean_run") as finish,
            patch(
                "track.v2.db.pipeline_store.insert_trajectory_points",
                return_value={("source", "1"): 101},
            ) as write,
        ):
            self.assertIs(store.get_clean_run("clean-1"), clean_record)
            self.assertEqual(
                store.start_clean_run(
                    source_id="source",
                    source_kind="csv",
                    parameters={"target_table": "trajectory_points"},
                ),
                "clean-1",
            )
            store.finish_clean("clean-1", status="completed")
            self.assertEqual(
                store.write_raw_points(
                    [_point("1")], table="raw_points", batch_size=17,
                ),
                {("source", "1"): 101},
            )

        fetch.assert_called_once_with(conn, "clean-1")
        start.assert_called_once_with(
            conn,
            source_id="source",
            source_kind="csv",
            parameters={"target_table": "trajectory_points"},
        )
        finish.assert_called_once_with(conn, "clean-1", status="completed")
        write.assert_called_once_with(
            conn, [_point("1")], table="raw_points", batch_size=17,
        )

    def test_clean_batch_preserves_kept_then_injected_order(self) -> None:
        conn = object()
        store = PostgresPipelineStore(conn)
        subtracks = [
            RefinementSubtrack(
                vehicle_id="vehicle-1",
                subtrack_index=99,
                points=[_point("1"), _point("2", generated=True), _point("3")],
            )
        ]
        pending_excluded = [object()]
        point_id_map = {("source", "1"): 101, ("source", "3"): 103}
        pending_point_keys = [("source", "1"), ("source", "3")]
        events: list[str] = []
        def kept(*args: object, **kwargs: object) -> int:
            events.append("kept")
            return 2

        def injected(*args: object, **kwargs: object) -> None:
            events.append("injected")

        with (
            patch("track.v2.db.pipeline_store.insert_kept_trajectory_points", side_effect=kept) as kept_mock,
            patch("track.v2.db.pipeline_store.insert_injected_points", side_effect=injected) as injected_mock,
        ):
            result = store.persist_clean_batch(
                "clean-1",
                "trajectory_points",
                point_id_map,
                pending_excluded,
                subtracks,
                pending_point_keys,
                10,
                batch_size=17,
            )

        self.assertEqual(events, ["kept", "injected"])
        self.assertEqual(
            result,
            CleanBatchResult(next_subtrack_index=11, kept_count=2),
        )
        self.assertEqual(point_id_map, {})
        self.assertEqual(pending_excluded, [])
        self.assertEqual(subtracks, [])
        self.assertEqual(pending_point_keys, [])
        assigned = kept_mock.call_args.args[1]
        self.assertEqual([item.subtrack_index for item in assigned], [10])
        self.assertIs(assigned, injected_mock.call_args.args[1])

    def test_clean_batch_writer_error_is_not_swallowed(self) -> None:
        conn = object()
        store = PostgresPipelineStore(conn)
        subtracks = [
            RefinementSubtrack(
                vehicle_id="vehicle-1", subtrack_index=1, points=[_point("1")],
            )
        ]
        with patch(
            "track.v2.db.pipeline_store.insert_kept_trajectory_points",
            side_effect=ValueError("kept failed"),
        ):
            with self.assertRaisesRegex(ValueError, "kept failed"):
                store.persist_clean_batch(
                    "clean-1", "trajectory_points", {}, [], subtracks, [], 1,
                )


class PipelineStoreDetectTest(unittest.TestCase):
    def test_persist_detect_uses_fixed_foreign_key_order_and_returns_maps(self) -> None:
        conn = object()
        store = PostgresPipelineStore(conn)
        turn_member = SimpleNamespace(source="turn", event_calculate_index=1)
        cross_member = SimpleNamespace(
            source="orthogonal_cross_track",
            vehicle_id="A|B",
            from_source_id="table_a",
            from_source_row_key="1,2",
            to_source_id="table_b",
            to_source_row_key="3,4",
        )
        cluster_a = SimpleNamespace(
            cluster_id=10,
            events=(turn_member, cross_member),
            crossing_points=(object(),),
        )
        cluster_b = SimpleNamespace(
            cluster_id=20,
            events=(),
            crossing_points=(),
        )
        result = SimpleNamespace(
            cross_track_candidates=[object()],
            turn_events=[object(), object()],
            clusters=[cluster_a, cluster_b],
            topologies=[object()],
            observations=[
                SimpleNamespace(cluster_id=10),
                SimpleNamespace(cluster_id=20),
                SimpleNamespace(cluster_id=99),
            ],
        )
        events: list[str] = []

        def record(name: str, value: object):
            def call(*args: object, **kwargs: object) -> object:
                events.append(name)
                return value

            return call

        with (
            patch(
                "track.v2.db.pipeline_store.insert_tracks_cross",
                side_effect=record("tracks_cross", 7),
            ) as tracks_cross,
            patch(
                "track.v2.db.pipeline_store.insert_turn_events",
                side_effect=record("turn_events", 2),
            ) as turn_events,
            patch(
                "track.v2.db.pipeline_store.insert_candidate_intersections",
                side_effect=record("candidate_intersections", {10: 110, 20: 120}),
            ) as candidate_intersections,
            patch(
                "track.v2.db.pipeline_store.insert_candidate_entry_exit_points",
                side_effect=record("candidate_entry_exit_points", {(10, 1): 1001}),
            ) as candidate_entry_exit_points,
            patch(
                "track.v2.db.pipeline_store.insert_intersections",
                side_effect=record("intersections", {10: 111}),
            ) as intersections,
            patch(
                "track.v2.db.pipeline_store.insert_entry_exit_points",
                side_effect=record("entry_exit_points", None),
            ) as entry_exit_points,
        ):
            persisted = store.persist_detect("detect-1", result, batch_size=17)

        self.assertEqual(
            events,
            [
                "candidate_intersections",
                "tracks_cross",
                "turn_events",
                "candidate_entry_exit_points",
                "intersections",
                "entry_exit_points",
            ],
        )
        self.assertEqual(
            persisted,
            DetectPersistenceResult(
                tracks_cross_count=7,
                candidate_intersection_ids={10: 110, 20: 120},
                candidate_entry_exit_ids={(10, 1): 1001},
                intersection_ids={10: 111},
                turn_event_count=2,
                candidate_intersection_count=2,
                candidate_entry_exit_point_count=1,
                intersection_count=1,
                entry_exit_point_count=1,
            ),
        )
        tracks_cross.assert_called_once_with(
            conn,
            result.cross_track_candidates,
            detect_id="detect-1",
            candidate_intersection_ids={
                ("A|B", "table_a", "1,2", "table_b", "3,4"): 110,
            },
            batch_size=17,
        )
        turn_events.assert_called_once_with(
            conn,
            result.turn_events,
            detect_id="detect-1",
            candidate_intersection_ids_by_calculate_index={1: 110},
            batch_size=17,
        )
        intersections.assert_called_once_with(
            conn,
            result.topologies,
            {10: 110, 20: 120},
            {10: cluster_a, 20: cluster_b},
            detect_id="detect-1",
        )
        entry_exit_points.assert_called_once_with(
            conn,
            result.observations,
            {10: 111},
            {(10, 1): 1001},
            detect_id="detect-1",
            batch_size=17,
        )

    def test_orthogonal_member_without_detect_entity_is_not_assigned(self) -> None:
        cluster = SimpleNamespace(
            cluster_id=10,
            events=(SimpleNamespace(source="orthogonal"),),
        )

        self.assertEqual(_candidate_memberships([cluster], {10: 110}), ({}, {}))

    def test_detect_writer_error_propagates_without_later_calls(self) -> None:
        conn = object()
        store = PostgresPipelineStore(conn)
        result = SimpleNamespace(
            cross_track_candidates=[],
            turn_events=[],
            clusters=[],
            topologies=[],
            observations=[],
        )
        with (
            patch(
                "track.v2.db.pipeline_store.insert_candidate_intersections",
                return_value={},
            ),
            patch(
                "track.v2.db.pipeline_store.insert_tracks_cross",
                side_effect=RuntimeError("detect failed"),
            ),
            patch("track.v2.db.pipeline_store.insert_turn_events") as turn_events,
        ):
            with self.assertRaisesRegex(RuntimeError, "detect failed"):
                store.persist_detect("detect-1", result)
        turn_events.assert_not_called()


class PipelineStoreClipFitTest(unittest.TestCase):
    def test_clip_and_fit_operations_delegate_and_return_values(self) -> None:
        conn = object()
        store = PostgresPipelineStore(conn)
        clip_config = ClipConfig()
        fitting_config = FittingConfig()
        clips = object()
        bundle = object()
        with (
            patch(
                "track.v2.db.pipeline_store.extract_clips_for_detect",
                return_value=clips,
            ) as load_clip,
            patch(
                "track.v2.db.pipeline_store.create_detect_run",
                return_value="detect-1",
            ) as start_detect,
            patch(
                "track.v2.db.pipeline_store.finish_detect_run",
            ) as finish_detect,
            patch(
                "track.v2.db.pipeline_store.create_fit_run",
                return_value="fit-1",
            ) as start_fit,
            patch(
                "track.v2.db.pipeline_store.insert_centerline_bundle",
                return_value=(101, 102),
            ) as persist_fit,
            patch("track.v2.db.pipeline_store.finish_fit_run") as finish_fit,
        ):
            self.assertEqual(
                store.start_detect_run(clean_id="clean-1", parameters={"x": 1}),
                "detect-1",
            )
            store.finish_detect("detect-1", status="completed")
            self.assertIs(
                store.load_clip("detect-1", config=clip_config), clips,
            )
            self.assertEqual(
                store.start_fit_run(
                    detect_id="detect-1",
                    clip_config=clip_config,
                    fitting_config=fitting_config,
                ),
                "fit-1",
            )
            self.assertEqual(
                store.persist_fit_bundle("fit-1", "detect-1", bundle, clips),
                (101, 102),
            )
            store.finish_fit("fit-1", status="completed", report={"ok": True})

        start_detect.assert_called_once_with(
            conn, clean_id="clean-1", parameters={"x": 1},
        )
        finish_detect.assert_called_once_with(conn, "detect-1", status="completed")
        load_clip.assert_called_once_with(conn, "detect-1", config=clip_config)
        start_fit.assert_called_once_with(
            conn,
            detect_id="detect-1",
            clip_config=clip_config,
            fitting_config=fitting_config,
        )
        persist_fit.assert_called_once_with(
            conn, bundle, clips, fit_id="fit-1", detect_id="detect-1",
        )
        finish_fit.assert_called_once_with(
            conn, "fit-1", status="completed", report={"ok": True},
        )

    def test_fit_writer_error_propagates(self) -> None:
        store = PostgresPipelineStore(object())
        with patch(
            "track.v2.db.pipeline_store.insert_centerline_bundle",
            side_effect=RuntimeError("fit failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "fit failed"):
                store.persist_fit_bundle("fit-1", "detect-1", object(), object())


if __name__ == "__main__":
    unittest.main()
