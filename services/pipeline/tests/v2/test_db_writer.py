"""Database-writer regression tests for verified intersections."""

from __future__ import annotations

import unittest
from dataclasses import replace

from track.v2.db_writer import (
    create_clean_run,
    create_detect_run,
    create_fit_run,
    finish_clean_run,
    finish_detect_run,
    insert_candidate_intersection_events,
    insert_candidate_intersections,
    insert_intersections,
    insert_injected_points,
    insert_kept_trajectory_points,
    insert_tracks_cross,
)
from track.v2.models import (
    ClipConfig,
    CrossTrackCandidate,
    DerivedPoint,
    GeneratedReason,
    HotspotCluster,
    IntersectionTopology,
    FittingConfig,
    PointRef,
    RefinementSubtrack,
    TrajectoryPoint,
    TurnEvent,
)


class _RecordingCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...]]] = []
        self.copy_rows: list[tuple[object, ...]] = []

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.executions.append((sql, params))

    def copy(self, sql: str) -> _RecordingCopy:
        return _RecordingCopy(self, sql)

    def fetchone(self) -> tuple[int, int]:
        params = self.executions[-1][1]
        if "INSERT INTO intersections" in self.executions[-1][0]:
            return len(self.executions), params[2]  # type: ignore[return-value]
        return len(self.executions), params[1]  # type: ignore[return-value]

    def fetchall(self) -> list[tuple[int, ...]]:
        sql, params = self.executions[-1]
        if "INSERT INTO candidate_intersections" in sql:
            row_count = sql.count("ST_Buffer(")
            width = len(params) // row_count
            return [
                (index + 1, params[index * width + 1])
                for index in range(row_count)
            ]
        if "INSERT INTO intersections" in sql:
            row_count = sql.count("CASE WHEN")
            width = len(params) // row_count
            return [
                (index + 1, params[index * width + 2])
                for index in range(row_count)
            ]
        return [(1,)]


class _RecordingCopy:
    def __init__(self, cursor: _RecordingCursor, sql: str) -> None:
        self.cursor = cursor
        self.sql = sql

    def __enter__(self) -> _RecordingCopy:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def write_row(self, row: tuple[object, ...]) -> None:
        self.cursor.copy_rows.append(row)


class _RecordingConnection:
    def __init__(self) -> None:
        self.recording_cursor = _RecordingCursor()

    def cursor(self) -> _RecordingCursor:
        return self.recording_cursor


def _cluster(cluster_id: int) -> HotspotCluster:
    return HotspotCluster(
        cluster_id=cluster_id,
        centroid_x=0.0,
        centroid_y=0.0,
        centroid_lon=116.0,
        centroid_lat=39.0,
        radius_m=100.0,
    )


def _trajectory_point(row_key: str, *, generated: bool = False) -> TrajectoryPoint:
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
        vehicle_id="vehicle-1",
        timestamp=float(row_key),
        lon=116.0,
        lat=39.0,
        heading=0.0,
        speed=10.0,
    )


class CleanerMembershipWriterTest(unittest.TestCase):
    def test_kept_and_injected_rows_share_subtrack_index_and_anchor_order(self) -> None:
        subtracks = [
            RefinementSubtrack(
                vehicle_id="vehicle-1",
                subtrack_index=7,
                points=[
                    _trajectory_point("1"),
                    _trajectory_point("15", generated=True),
                    _trajectory_point("2"),
                ],
            )
        ]
        conn = _RecordingConnection()

        kept_count = insert_kept_trajectory_points(
            conn,
            subtracks,
            {("raw_points", "1"): 101, ("raw_points", "2"): 102},
            clean_id="clean-1",
        )
        insert_injected_points(conn, subtracks, clean_id="clean-1")

        self.assertEqual(kept_count, 2)
        self.assertEqual(
            [(row[5], row[6]) for row in conn.recording_cursor.copy_rows],
            [(7, 0), (7, 1)],
        )
        _, injected_params = conn.recording_cursor.executions[-1]
        self.assertEqual(len(injected_params), 20)
        self.assertEqual((injected_params[2], injected_params[3]), (7, 0))


class RunRegistryWriterTest(unittest.TestCase):
    class _Cursor:
        def __init__(self, row):
            self.row = row
            self.sql = None
            self.params = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, sql, params):
            self.sql = sql
            self.params = params

        def fetchone(self):
            return self.row

    class _Conn:
        def __init__(self, row):
            self.cursor_instance = RunRegistryWriterTest._Cursor(row)

        def cursor(self):
            return self.cursor_instance

    def test_clean_and_detect_runs_have_distinct_ids_and_explicit_parent(self):
        conn = self._Conn(None)

        clean_id = create_clean_run(
            conn, source_id="table_x", source_kind="postgres_table",
            parameters={"target_table": "table_x"},
        )
        clean_sql = conn.cursor_instance.sql
        clean_params = conn.cursor_instance.params
        detect_id = create_detect_run(
            conn, clean_id=clean_id, parameters={"window": [2000, 2000]}
        )

        self.assertNotEqual(clean_id, detect_id)
        self.assertIn("INSERT INTO clean_runs", clean_sql)
        self.assertEqual(clean_params[0], clean_id)
        self.assertIn("INSERT INTO detect_runs", conn.cursor_instance.sql)
        self.assertEqual(conn.cursor_instance.params[:2], (detect_id, clean_id))

    def test_stage_completion_updates_only_its_registry(self):
        conn = self._Conn(None)

        finish_clean_run(conn, "clean-1", status="completed")
        self.assertIn("UPDATE clean_runs", conn.cursor_instance.sql)
        self.assertEqual(conn.cursor_instance.params, ("completed", "clean-1"))
        finish_detect_run(conn, "detect-1", status="completed")
        self.assertIn("UPDATE detect_runs", conn.cursor_instance.sql)
        self.assertEqual(conn.cursor_instance.params, ("completed", "detect-1"))

    def test_fit_run_requires_completed_detect_and_each_call_gets_a_new_id(self):
        conn = self._Conn(("created",))

        first = create_fit_run(
            conn, detect_id="detect-1", clip_config=ClipConfig(),
            fitting_config=FittingConfig(),
        )
        first_sql = conn.cursor_instance.sql
        second = create_fit_run(
            conn, detect_id="detect-1", clip_config=ClipConfig(),
            fitting_config=FittingConfig(),
        )

        self.assertNotEqual(first, second)
        self.assertIn("d.status = 'completed'", first_sql)
        self.assertEqual(conn.cursor_instance.params[-1], "detect-1")

        with self.assertRaisesRegex(ValueError, "not completed"):
            create_fit_run(
                self._Conn(None), detect_id="running-detect",
                clip_config=ClipConfig(), fitting_config=FittingConfig(),
            )


class InsertIntersectionsTest(unittest.TestCase):
    def test_only_complete_topologies_are_written(self) -> None:
        conn = _RecordingConnection()
        incomplete = IntersectionTopology(
            cluster_id=10,
            branch_centers_deg=[0.0],
            transition_counts={},
            observation_count=1,
            is_complete=False,
            reason="insufficient observations",
        )
        complete = IntersectionTopology(
            cluster_id=20,
            branch_centers_deg=[0.0, 90.0, 180.0],
            transition_counts={(0, 1): 3},
            observation_count=8,
            is_complete=True,
            reason="complete",
            clip_radius_m=145.0,
        )

        result = insert_intersections(
            conn,
            [incomplete, complete],
            {10: 110, 20: 120},
            {10: _cluster(10), 20: _cluster(20)},
            detect_id="detect-1",
        )

        self.assertEqual(result, {20: 1})
        self.assertEqual(len(conn.recording_cursor.executions), 1)
        sql, params = conn.recording_cursor.executions[0]
        self.assertIn("clip_radius_m", sql)
        self.assertEqual(params[2], 20)
        self.assertIs(params[6], True)
        self.assertEqual(params[8], 145.0)
        self.assertEqual(params[-1], 145.0)

    def test_complete_topologies_are_inserted_in_one_batch(self) -> None:
        conn = _RecordingConnection()
        topologies = [
            IntersectionTopology(
                cluster_id=cluster_id,
                branch_centers_deg=[0.0, 90.0],
                transition_counts={(0, 1): 2},
                observation_count=4,
                is_complete=True,
                reason="complete",
                clip_radius_m=100.0,
            )
            for cluster_id in (10, 20)
        ]

        result = insert_intersections(
            conn,
            topologies,
            {10: 110, 20: 120},
            {10: _cluster(10), 20: _cluster(20)},
            detect_id="detect-1",
        )

        self.assertEqual(result, {10: 1, 20: 2})
        self.assertEqual(len(conn.recording_cursor.executions), 1)


class InsertTracksCrossTest(unittest.TestCase):
    def test_writes_deduplicated_cross_track_candidate_with_provenance(self) -> None:
        candidate = CrossTrackCandidate(
            point_ref=PointRef("table_a", "102", stable=True),
            vehicle_id="A|B",
            timestamp=15.0,
            lon=116.0,
            lat=39.0,
            x=1.5,
            y=2.5,
            heading=0.0,
            speed=12.0,
            from_source_id="table_a",
            from_source_row_key="102,105",
            to_source_id="table_b",
            to_source_row_key="77,78",
            origin_lon=116.0,
            origin_lat=39.0,
        )
        conn = _RecordingConnection()

        written = insert_tracks_cross(conn, [candidate], detect_id="detect-1")

        self.assertEqual(written, 1)
        sql, params = conn.recording_cursor.executions[0]
        self.assertIn("INSERT INTO tracks_cross", sql)
        self.assertIn(
            "ON CONFLICT (detect_id, vehicle_id, from_source_id, from_source_row_key, "
            "to_source_id, to_source_row_key) DO NOTHING",
            sql,
        )
        self.assertEqual(
            params,
            [
                "detect-1", "A|B", 15.0, 116.0, 39.0, 1.5, 2.5, 0.0, 12.0,
                "table_a", "102,105", "table_b", "77,78",
                "orthogonal_cross_track", 116.0, 39.0,
            ],
        )

    def test_identical_derived_candidate_is_persisted_independently_per_run(self) -> None:
        candidate = CrossTrackCandidate(
            point_ref=PointRef("table_a", "102", stable=True),
            vehicle_id="A|B", timestamp=15.0, lon=116.0, lat=39.0,
            x=1.5, y=2.5, heading=0.0, speed=12.0,
            from_source_id="table_a", from_source_row_key="102",
            to_source_id="table_b", to_source_row_key="77",
            origin_lon=116.0, origin_lat=39.0,
        )
        conn = _RecordingConnection()

        insert_tracks_cross(conn, [candidate], detect_id="detect-1")
        insert_tracks_cross(conn, [candidate], detect_id="detect-2")

        self.assertEqual(
            [params[0] for _, params in conn.recording_cursor.executions],
            ["detect-1", "detect-2"],
        )


class CandidateIntersectionEventsTest(unittest.TestCase):
    def test_non_turn_members_do_not_collide_with_turn_event_ids(self) -> None:
        cluster = _cluster(5)
        turn_event = TurnEvent(
            event_id=1, vehicle_id="turn", timestamp=1.0,
            lon=116.0, lat=39.0, x=0.0, y=0.0,
            in_bearing=0.0, out_bearing=90.0, turn_deg=90.0, score=90.0,
            source="turn",
        )
        cross_track_event = TurnEvent(
            event_id=0, vehicle_id="A|B", timestamp=2.0,
            lon=116.0, lat=39.0, x=0.0, y=0.0,
            in_bearing=0.0, out_bearing=0.0, turn_deg=0.0, score=0.0,
            source="orthogonal_cross_track",
        )
        cluster = replace(cluster, events=(turn_event, cross_track_event))
        conn = _RecordingConnection()

        insert_candidate_intersection_events(
            conn, [cluster], {5: 55}, {0: 100, 1: 101}, detect_id="detect-1",
        )

        _, params = conn.recording_cursor.executions[0]
        self.assertEqual(
            params,
            [
                "detect-1", 55, "turn", 101, None, None, None, None,
                "detect-1", 55, "orthogonal_cross_track", None, None, None, None, None,
            ],
        )


class CandidateIntersectionSourcesTest(unittest.TestCase):
    def test_sources_column_contains_data_source_ids_and_detect_id(self) -> None:
        cluster = replace(
            _cluster(5),
            algorithm_sources=("turn", "orthogonal"),
            source_ids=("C:/input-a.csv", "project_raw_points"),
        )
        conn = _RecordingConnection()

        insert_candidate_intersections(conn, [cluster], detect_id="detect-1")

        sql, params = conn.recording_cursor.executions[0]
        self.assertIn("detect_id", sql)
        self.assertEqual(params[0], "detect-1")
        self.assertEqual(params[8], '["C:/input-a.csv", "project_raw_points"]')

    def test_candidates_are_inserted_in_one_batch(self) -> None:
        conn = _RecordingConnection()

        result = insert_candidate_intersections(
            conn,
            [_cluster(5), _cluster(6)],
            detect_id="detect-1",
        )

        self.assertEqual(result, {5: 1, 6: 2})
        self.assertEqual(len(conn.recording_cursor.executions), 1)


if __name__ == "__main__":
    unittest.main()
