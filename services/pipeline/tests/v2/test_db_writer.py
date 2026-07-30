"""Database-writer regression tests for verified intersections."""

from __future__ import annotations

import unittest
from dataclasses import replace

from track.v2.db_writer import (
    insert_candidate_intersection_events,
    insert_candidate_intersections,
    insert_intersections,
    insert_tracks_cross,
)
from track.v2.models import (
    CrossTrackCandidate,
    HotspotCluster,
    IntersectionTopology,
    PointRef,
    TurnEvent,
)


class _RecordingCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.executions.append((sql, params))

    def fetchone(self) -> tuple[int, int]:
        params = self.executions[-1][1]
        if "INSERT INTO intersections" in self.executions[-1][0]:
            return len(self.executions), params[2]  # type: ignore[return-value]
        return len(self.executions), params[1]  # type: ignore[return-value]

    def fetchall(self) -> list[tuple[int]]:
        return [(1,)]


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
        )

        result = insert_intersections(
            conn,
            [incomplete, complete],
            {10: 110, 20: 120},
            {10: _cluster(10), 20: _cluster(20)},
            run_id="run-1",
        )

        self.assertEqual(result, {20: 1})
        self.assertEqual(len(conn.recording_cursor.executions), 1)
        _, params = conn.recording_cursor.executions[0]
        self.assertEqual(params[2], 20)
        self.assertIs(params[6], True)


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

        written = insert_tracks_cross(conn, [candidate], run_id="run-1")

        self.assertEqual(written, 1)
        sql, params = conn.recording_cursor.executions[0]
        self.assertIn("INSERT INTO tracks_cross", sql)
        self.assertIn(
            "ON CONFLICT (run_id, vehicle_id, from_source_id, from_source_row_key, "
            "to_source_id, to_source_row_key) DO NOTHING",
            sql,
        )
        self.assertEqual(
            params,
            [
                "run-1", "A|B", 15.0, 116.0, 39.0, 1.5, 2.5, 0.0, 12.0,
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

        insert_tracks_cross(conn, [candidate], run_id="run-1")
        insert_tracks_cross(conn, [candidate], run_id="run-2")

        self.assertEqual(
            [params[0] for _, params in conn.recording_cursor.executions],
            ["run-1", "run-2"],
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
            conn, [cluster], {5: 55}, {0: 100, 1: 101}, run_id="run-1",
        )

        _, params = conn.recording_cursor.executions[0]
        self.assertEqual(
            params,
            [
                "run-1", 55, "turn", 101, None, None, None, None,
                "run-1", 55, "orthogonal_cross_track", None, None, None, None, None,
            ],
        )


class CandidateIntersectionSourcesTest(unittest.TestCase):
    def test_sources_column_contains_data_source_ids_and_run_id(self) -> None:
        cluster = replace(
            _cluster(5),
            algorithm_sources=("turn", "orthogonal"),
            source_ids=("C:/input-a.csv", "project_raw_points"),
        )
        conn = _RecordingConnection()

        insert_candidate_intersections(conn, [cluster], run_id="run-1")

        sql, params = conn.recording_cursor.executions[0]
        self.assertIn("run_id", sql)
        self.assertEqual(params[0], "run-1")
        self.assertEqual(params[8], '["C:/input-a.csv", "project_raw_points"]')


if __name__ == "__main__":
    unittest.main()
