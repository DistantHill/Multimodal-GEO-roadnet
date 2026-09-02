"""Database-writer regression tests for verified intersections."""

from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace

from track.v2.db_writer import (
    create_clean_run,
    create_detect_run,
    create_fit_run,
    fetch_clean_run,
    finish_clean_run,
    finish_detect_run,
    insert_candidate_entry_exit_points,
    insert_candidate_intersections,
    insert_entry_exit_points,
    insert_intersections,
    insert_injected_points,
    insert_kept_trajectory_points,
    insert_tracks_cross,
    insert_turn_events,
)
from track.v2.models import (
    ClipConfig,
    CrossingPoint,
    CrossTrackCandidate,
    DerivedPoint,
    EntryExitObservation,
    GeneratedReason,
    HotspotCluster,
    IntersectionTopology,
    FittingConfig,
    PointRef,
    RefinementSubtrack,
    SpatialContext,
    TrajectoryPoint,
    TurnEvent,
)
from track.v2.projection import ProjectionError


_SPATIAL_CONTEXT = SpatialContext(
    source_srid=4326,
    processing_srid=32650,
    projection_config={
        "coordinate_semantics_version": 2,
        "enforce_area_of_use": True,
        "max_scale_error": 0.01,
    },
)
_EXPECTED_X = 413407.3219726745
_EXPECTED_Y = 4317252.16462973


class _RecordingCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...]]] = []
        self.copy_rows: list[tuple[object, ...]] = []
        self.copy_statements: list[str] = []

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
        if "AS requested(injected_id, vehicle_id)" in sql:
            return list(zip(params[0], params[1]))  # type: ignore[arg-type,return-value]
        if "generate_series" in sql:
            return [
                (index + 1, index + 1)
                for index in range(int(params[0]))
            ]
        if "INSERT INTO candidate_intersections" in sql:
            row_count = sql.count("ST_Buffer(")
            width = len(params) // row_count
            return list(reversed([
                (int(params[index * width + 1]),)
                for index in range(row_count)
            ]))
        if "INSERT INTO intersections" in sql:
            row_count = sql.count("CASE WHEN")
            width = len(params) // row_count
            return list(reversed([
                (int(params[index * width + 1]),)
                for index in range(row_count)
            ]))
        return [(1,)]


class _RecordingCopy:
    def __init__(self, cursor: _RecordingCursor, sql: str) -> None:
        self.cursor = cursor
        self.sql = sql

    def __enter__(self) -> _RecordingCopy:
        self.cursor.copy_statements.append(self.sql)
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


def _cluster(cluster_calculate_index: int) -> HotspotCluster:
    return HotspotCluster(
        cluster_calculate_index=cluster_calculate_index,
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
        insert_injected_points(
            conn,
            subtracks,
            clean_id="clean-1",
            spatial_context=_SPATIAL_CONTEXT,
        )

        self.assertEqual(kept_count, 2)
        self.assertEqual(
            [(row[5], row[6]) for row in conn.recording_cursor.copy_rows],
            [(7, 0), (7, 1)],
        )
        _, injected_params = conn.recording_cursor.executions[-1]
        self.assertEqual(len(injected_params), 18)
        self.assertEqual((injected_params[2], injected_params[3]), (7, 0))
        self.assertAlmostEqual(injected_params[9], _EXPECTED_X)
        self.assertAlmostEqual(injected_params[10], _EXPECTED_Y)
        self.assertNotIn("origin_lon", conn.recording_cursor.executions[-1][0])


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
        conn = self._Conn(("created",))

        clean_id = create_clean_run(
            conn, source_id="table_x", source_kind="postgres_table",
            parameters={"target_table": "table_x"},
            spatial_context=_SPATIAL_CONTEXT,
        )
        clean_sql = conn.cursor_instance.sql
        clean_params = conn.cursor_instance.params
        detect_id = create_detect_run(
            conn, clean_id=clean_id, parameters={"window": [2000, 2000]}
        )

        self.assertNotEqual(clean_id, detect_id)
        self.assertIn("INSERT INTO clean_runs", clean_sql)
        self.assertEqual(clean_params[0], clean_id)
        self.assertEqual(clean_params[3:5], (4326, 32650))
        self.assertEqual(
            clean_params[5],
            '{"coordinate_semantics_version": 2, "enforce_area_of_use": true, '
            '"max_scale_error": 0.01}',
        )
        self.assertIn("INSERT INTO detect_runs", conn.cursor_instance.sql)
        self.assertIn("c.status = 'completed'", conn.cursor_instance.sql)
        self.assertIn("coordinate_semantics_version", conn.cursor_instance.sql)
        self.assertEqual(conn.cursor_instance.params[0], detect_id)
        self.assertEqual(conn.cursor_instance.params[-1], clean_id)

    def test_detect_run_rejects_incomplete_or_unmigrated_clean(self):
        with self.assertRaisesRegex(ValueError, "fixed-CRS coordinates"):
            create_detect_run(
                self._Conn(None),
                clean_id="legacy-clean",
                parameters={},
            )

    def test_fetch_clean_run_materializes_its_spatial_context(self):
        conn = self._Conn((
            "clean-1",
            "table_x",
            "postgres_table",
            {"target_table": "table_x"},
            "completed",
            4326,
            32650,
            _SPATIAL_CONTEXT.projection_config,
        ))

        record = fetch_clean_run(conn, "clean-1")

        self.assertEqual(record.spatial_context, _SPATIAL_CONTEXT)
        self.assertIn("source_srid, processing_srid, projection_config", conn.cursor_instance.sql)

    def test_fetch_clean_run_rejects_legacy_null_processing_srid(self):
        conn = self._Conn((
            "clean-legacy", "table_x", "postgres_table", {}, "completed",
            4326, None, {},
        ))

        with self.assertRaisesRegex(ProjectionError, "no processing_srid"):
            fetch_clean_run(conn, "clean-legacy")

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
        self.assertIn("coordinate_semantics_version", first_sql)
        self.assertEqual(first_sql.count("'v2'"), 2)
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
            cluster_calculate_index=10,
            branch_centers_deg=[0.0],
            transition_counts={},
            observation_count=1,
            is_complete=False,
            reason="insufficient observations",
        )
        complete = IntersectionTopology(
            cluster_calculate_index=20,
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
            spatial_context=_SPATIAL_CONTEXT,
        )

        self.assertEqual(result, {20: 1})
        self.assertEqual(len(conn.recording_cursor.executions), 2)
        sql, params = conn.recording_cursor.executions[-1]
        self.assertIn("clip_radius_m", sql)
        self.assertNotIn("cluster_id", sql)
        self.assertEqual(params[1], 1)
        self.assertEqual(params[2], 120)
        self.assertIs(params[6], True)
        self.assertEqual(params[8], 145.0)
        self.assertAlmostEqual(params[10], _EXPECTED_X)
        self.assertAlmostEqual(params[11], _EXPECTED_Y)
        self.assertEqual(params[12], 32650)
        self.assertEqual(params[-1], 145.0)
        self.assertIn("ST_Transform(ST_Buffer", sql)

    def test_complete_topologies_are_inserted_in_one_batch(self) -> None:
        conn = _RecordingConnection()
        topologies = [
            IntersectionTopology(
                cluster_calculate_index=cluster_calculate_index,
                branch_centers_deg=[0.0, 90.0],
                transition_counts={(0, 1): 2},
                observation_count=4,
                is_complete=True,
                reason="complete",
                clip_radius_m=100.0,
            )
            for cluster_calculate_index in (20, 10)
        ]

        result = insert_intersections(
            conn,
            topologies,
            {10: 110, 20: 120},
            {10: _cluster(10), 20: _cluster(20)},
            detect_id="detect-1",
            spatial_context=_SPATIAL_CONTEXT,
        )

        self.assertEqual(result, {10: 1, 20: 2})
        self.assertEqual(len(conn.recording_cursor.executions), 2)


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
        )
        conn = _RecordingConnection()

        written = insert_tracks_cross(
            conn,
            [candidate],
            detect_id="detect-1",
            candidate_intersection_ids={
                ("A|B", "table_a", "102,105", "table_b", "77,78"): 55,
            },
            spatial_context=_SPATIAL_CONTEXT,
        )

        self.assertEqual(written, 1)
        copy_sql = conn.recording_cursor.copy_statements[0]
        self.assertIn("COPY tracks_cross", copy_sql)
        self.assertEqual(
            conn.recording_cursor.copy_rows[0],
            (
                "detect-1", "A|B", 15.0, 116.0, 39.0,
                _EXPECTED_X, _EXPECTED_Y, 0.0, 12.0,
                "table_a", "102,105", "table_b", "77,78",
                "orthogonal_cross_track", 55,
            ),
        )
        self.assertNotIn("origin_lon", copy_sql)

    def test_identical_derived_candidate_is_persisted_independently_per_run(self) -> None:
        candidate = CrossTrackCandidate(
            point_ref=PointRef("table_a", "102", stable=True),
            vehicle_id="A|B", timestamp=15.0, lon=116.0, lat=39.0,
            x=1.5, y=2.5, heading=0.0, speed=12.0,
            from_source_id="table_a", from_source_row_key="102",
            to_source_id="table_b", to_source_row_key="77",
        )
        conn = _RecordingConnection()

        insert_tracks_cross(
            conn, [candidate], detect_id="detect-1",
            spatial_context=_SPATIAL_CONTEXT,
        )
        insert_tracks_cross(
            conn, [candidate], detect_id="detect-2",
            spatial_context=_SPATIAL_CONTEXT,
        )

        self.assertEqual(
            [row[0] for row in conn.recording_cursor.copy_rows],
            ["detect-1", "detect-2"],
        )


class InsertTurnEventsTest(unittest.TestCase):
    def test_copy_omits_calculate_index_and_returns_inserted_count(self) -> None:
        events = [
            TurnEvent(
                event_calculate_index=calculate_index,
                vehicle_id=f"vehicle-{calculate_index}",
                timestamp=float(calculate_index),
                lon=116.0,
                lat=39.0,
                x=0.0,
                y=0.0,
                in_bearing=0.0,
                out_bearing=90.0,
                turn_deg=90.0,
                score=90.0,
            )
            for calculate_index in (10, 20)
        ]
        conn = _RecordingConnection()

        inserted_count = insert_turn_events(
            conn,
            events,
            detect_id="detect-1",
            candidate_intersection_ids_by_calculate_index={10: 55},
            spatial_context=_SPATIAL_CONTEXT,
        )

        copy_sql = conn.recording_cursor.copy_statements[0]
        self.assertIn("COPY turn_events", copy_sql)
        self.assertNotIn("event_id", copy_sql)
        self.assertNotIn("event_calculate_index", copy_sql)
        self.assertEqual(inserted_count, 2)
        self.assertEqual(
            [row[-1] for row in conn.recording_cursor.copy_rows],
            [55, None],
        )
        self.assertEqual(conn.recording_cursor.executions, [])


class CandidateIntersectionSourcesTest(unittest.TestCase):
    def test_sources_column_contains_data_source_ids_and_detect_id(self) -> None:
        cluster = replace(
            _cluster(5),
            algorithm_sources=("turn", "orthogonal"),
            source_ids=("C:/input-a.csv", "project_raw_points"),
        )
        conn = _RecordingConnection()

        insert_candidate_intersections(
            conn, [cluster], detect_id="detect-1",
            spatial_context=_SPATIAL_CONTEXT,
        )

        sql, params = conn.recording_cursor.executions[-1]
        self.assertIn("detect_id", sql)
        self.assertNotIn("cluster_id", sql)
        self.assertEqual(params[0], "detect-1")
        self.assertEqual(params[8], '["C:/input-a.csv", "project_raw_points"]')
        self.assertAlmostEqual(params[2], _EXPECTED_X)
        self.assertAlmostEqual(params[3], _EXPECTED_Y)
        self.assertEqual(params[-2], 32650)
        self.assertIn("ST_Transform(ST_Buffer", sql)
        self.assertNotIn("origin_lon", sql)

    def test_candidates_are_inserted_in_one_batch(self) -> None:
        conn = _RecordingConnection()

        result = insert_candidate_intersections(
            conn,
            [_cluster(6), _cluster(5)],
            detect_id="detect-1",
            spatial_context=_SPATIAL_CONTEXT,
        )

        self.assertEqual(result, {5: 1, 6: 2})
        self.assertEqual(len(conn.recording_cursor.executions), 2)

    def test_duplicate_calculation_indexes_are_rejected_before_writing(self) -> None:
        conn = _RecordingConnection()

        with self.assertRaisesRegex(ValueError, "calculation indexes must be unique"):
            insert_candidate_intersections(
                conn,
                [_cluster(5), _cluster(5)],
                detect_id="detect-1",
                spatial_context=_SPATIAL_CONTEXT,
            )

        self.assertEqual(conn.recording_cursor.executions, [])


class CandidateChildIdentityTest(unittest.TestCase):
    def test_crossing_writer_requires_detect_id(self) -> None:
        conn = _RecordingConnection()

        with self.assertRaisesRegex(
            ValueError,
            "detect_id is required for derived v2 persistence",
        ):
            insert_candidate_entry_exit_points(
                conn,
                [],
                {},
                spatial_context=_SPATIAL_CONTEXT,
            )

        self.assertEqual(conn.recording_cursor.executions, [])

    def test_crossing_and_observation_rows_omit_calculation_index(self) -> None:
        cluster = replace(
            _cluster(5),
            crossing_points=(SimpleNamespace(
                crossing_id=7,
                vehicle_id="vehicle-1",
                segment_index=3,
                is_entry=True,
                crossing_lon=116.0,
                crossing_lat=39.0,
                point_ref=PointRef(
                    "injected_points",
                    "282",
                    stable=False,
                    generated_reason="orthogonal_heading",
                ),
                end_point_ref=PointRef(
                    "public.injected_points",
                    "283",
                    stable=False,
                    generated_reason="orthogonal_heading",
                ),
            ),),
        )
        conn = _RecordingConnection()

        crossing_ids = insert_candidate_entry_exit_points(
            conn,
            [cluster],
            {5: 55},
            detect_id="detect-1",
            spatial_context=_SPATIAL_CONTEXT,
        )
        insert_entry_exit_points(
            conn,
            [
                EntryExitObservation(
                    5,
                    "vehicle-1",
                    0.0,
                    90.0,
                    0.0,
                    90.0,
                    90.0,
                    entry_crossing_id=0,
                    exit_crossing_id=7,
                )
            ],
            {5: 65},
            {(5, 0): 70, **crossing_ids},
            detect_id="detect-1",
        )

        crossing_sql, crossing_params = next(
            (sql, params)
            for sql, params in conn.recording_cursor.executions
            if "INSERT INTO candidate_entry_exit_points" in sql
        )
        self.assertNotIn("cluster_id", crossing_sql)
        self.assertEqual(
            tuple(crossing_params[-4:]),
            (
                "public.injected_points",
                "282",
                "public.injected_points",
                "283",
            ),
        )
        observation_sql = conn.recording_cursor.copy_statements[-1]
        self.assertIn("intersection_id", observation_sql)
        self.assertNotIn("cluster_id", observation_sql)
        observation_row = conn.recording_cursor.copy_rows[-1]
        self.assertEqual(observation_row[-2:], (70, 1))

    def test_unpersisted_injected_crossing_reference_is_rejected(self) -> None:
        cluster = replace(
            _cluster(5),
            crossing_points=(SimpleNamespace(
                crossing_id=0,
                vehicle_id="vehicle-1",
                segment_index=0,
                is_entry=True,
                crossing_lon=116.0,
                crossing_lat=39.0,
                point_ref=PointRef(
                    "public.raw_points",
                    "10->11:orthogonal_heading",
                    stable=False,
                    generated_reason="orthogonal_heading",
                ),
                end_point_ref=PointRef("public.raw_points", "11", stable=True),
            ),),
        )
        conn = _RecordingConnection()

        with self.assertRaisesRegex(ValueError, "must be reloaded"):
            insert_candidate_entry_exit_points(
                conn,
                [cluster],
                {5: 55},
                detect_id="detect-1",
                spatial_context=_SPATIAL_CONTEXT,
            )

        self.assertEqual(conn.recording_cursor.executions, [])

    def test_injected_crossing_reference_must_belong_to_detect_clean_vehicle(self) -> None:
        cluster = replace(
            _cluster(5),
            crossing_points=(SimpleNamespace(
                crossing_id=0,
                vehicle_id="vehicle-1",
                segment_index=0,
                is_entry=True,
                crossing_lon=116.0,
                crossing_lat=39.0,
                point_ref=PointRef(
                    "public.injected_points",
                    "282",
                    stable=False,
                    generated_reason="orthogonal_heading",
                ),
                end_point_ref=PointRef("public.raw_points", "11", stable=True),
            ),),
        )
        conn = _RecordingConnection()
        conn.recording_cursor.fetchall = lambda: []  # type: ignore[method-assign]

        with self.assertRaisesRegex(ValueError, "do not belong to Detect Run"):
            insert_candidate_entry_exit_points(
                conn,
                [cluster],
                {5: 55},
                detect_id="detect-1",
                spatial_context=_SPATIAL_CONTEXT,
            )

        validation_sql = conn.recording_cursor.executions[-1][0]
        self.assertIn("injected.clean_id = detect.clean_id", validation_sql)
        self.assertIn(
            "injected.vehicle_id = requested.vehicle_id",
            validation_sql,
        )
        self.assertNotIn("generate_series", validation_sql)


class ProjectedEventWriterTest(unittest.TestCase):
    def test_turn_event_reprojects_geographic_truth_and_omits_origin(self) -> None:
        event = TurnEvent(
            event_calculate_index=7,
            vehicle_id="vehicle-1",
            timestamp=10.0,
            lon=116.0,
            lat=39.0,
            x=-1.0,
            y=-2.0,
            in_bearing=0.0,
            out_bearing=90.0,
            turn_deg=90.0,
            score=90.0,
        )
        conn = _RecordingConnection()

        inserted_count = insert_turn_events(
            conn,
            [event],
            detect_id="detect-1",
            spatial_context=_SPATIAL_CONTEXT,
        )

        self.assertEqual(inserted_count, 1)
        copy_sql = conn.recording_cursor.copy_statements[0]
        row = conn.recording_cursor.copy_rows[0]
        self.assertAlmostEqual(row[5], _EXPECTED_X)
        self.assertAlmostEqual(row[6], _EXPECTED_Y)
        self.assertNotIn("origin_lon", copy_sql)

    def test_crossing_reprojects_geographic_truth_and_omits_origin(self) -> None:
        crossing = CrossingPoint(
            crossing_id=9,
            cluster_ref="cluster-5",
            vehicle_id="vehicle-1",
            point_ref=PointRef("raw_points", "1", stable=True),
            segment_index=3,
            crossing_x=-1.0,
            crossing_y=-2.0,
            crossing_lon=116.0,
            crossing_lat=39.0,
            is_entry=True,
            end_point_ref=PointRef("raw_points", "2", stable=True),
        )
        cluster = replace(_cluster(5), crossing_points=(crossing,))
        conn = _RecordingConnection()

        mapping = insert_candidate_entry_exit_points(
            conn,
            [cluster],
            {5: 55},
            detect_id="detect-1",
            spatial_context=_SPATIAL_CONTEXT,
        )

        self.assertEqual(mapping, {(5, 9): 1})
        sql, params = next(
            (sql, params)
            for sql, params in conn.recording_cursor.executions
            if "INSERT INTO candidate_entry_exit_points" in sql
        )
        self.assertAlmostEqual(params[6], _EXPECTED_X)
        self.assertAlmostEqual(params[7], _EXPECTED_Y)
        self.assertNotIn("origin_lon", sql)


if __name__ == "__main__":
    unittest.main()
