"""Public replay-service behaviour tests."""

from __future__ import annotations

import csv
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from track.v2.replay import (
    PostgresReplayStore,
    PostgresSourceRowResolver,
    ReplayService,
    ReplayTarget,
    _interpolate_heading,
    _replay_fit_endpoint,
    replay_centerline_inputs,
)
from track.v2.models import PointRef, SpatialContext, TrajectoryPoint
from track.v2.projection import CoordinateTransformer
from track.v2.sources import source_row_hash
from track.v2.cli.replay import _target_from_args


_REPLAY_CONTEXT = SpatialContext(
    source_srid=4326,
    processing_srid=3857,
    projection_config={"coordinate_semantics_version": 2, "enforce_area_of_use": True, "max_scale_error": 0.2},
)
_REPLAY_TRANSFORMER = CoordinateTransformer(_REPLAY_CONTEXT)


class _MemoryStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, int], dict[str, object]] = {}
        self.links: dict[tuple[str, int], list[tuple[str, ReplayTarget]]] = {}

    def get_entity(self, target: ReplayTarget) -> dict[str, object] | None:
        return self.rows.get((target.kind, target.id))

    def parent_refs(
        self,
        target: ReplayTarget,
        row: dict[str, object],
    ) -> list[tuple[str, ReplayTarget]]:
        return self.links.get((target.kind, target.id), [])


class _LookupCursor:
    def __init__(self) -> None:
        self.sql = ""
        self.params: tuple[object, ...] = ()
        self.executions: list[tuple[str, object]] = []

    def __enter__(self) -> _LookupCursor:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.sql = sql
        self.params = params
        self.executions.append((sql, params))

    def fetchall(self) -> list[tuple[int]]:
        if "candidate_intersection_events" in self.sql:
            return [(15,)]
        if "candidate_entry_exit_points" in self.sql:
            return [(40,)]
        if "entry_exit_points" in self.sql:
            return [(50,)]
        return []

    def fetchone(self) -> tuple[int] | None:
        if self.sql.startswith("SELECT source_kind"):
            return ("csv", "trajectory_points")
        if self.sql.startswith("SELECT clean_id FROM detect_runs"):
            return ("clean-1",)
        if self.sql.startswith("SELECT trajectory_point_id"):
            return (self.params[-1],)
        if "trajectory_points" in self.sql:
            row_id = self.params[0]
            return {1: (1,), 2: (2,)}.get(row_id)
        return None


class _LookupConnection:
    def __init__(self) -> None:
        self.cursor_ = _LookupCursor()

    def cursor(self) -> _LookupCursor:
        return self.cursor_


class ReplayServiceTest(unittest.TestCase):
    def test_heading_interpolation_uses_shortest_circular_arc(self) -> None:
        self.assertAlmostEqual(_interpolate_heading(350.0, 10.0, 0.5), 0.0)
        self.assertAlmostEqual(_interpolate_heading(10.0, 350.0, 0.5), 0.0)

    def test_fit_endpoint_interpolates_in_processing_crs(self) -> None:
        context = SpatialContext(
            source_srid=4326,
            processing_srid=32650,
            projection_config={"coordinate_semantics_version": 2, "enforce_area_of_use": True, "max_scale_error": 0.01},
        )
        transformer = CoordinateTransformer(context)
        first_xy = transformer.forward(116.4, 39.9)
        second_xy = (first_xy[0] + 1_000.0, first_xy[1] + 400.0)

        def point(index: int, xy: tuple[float, float]) -> TrajectoryPoint:
            lon, lat = transformer.inverse(*xy)
            return TrajectoryPoint(
                PointRef("replay", str(index), True),
                "vehicle-1",
                float(index * 10),
                lon,
                lat,
                90.0,
                10.0,
                x=xy[0],
                y=xy[1],
            )

        endpoint = _replay_fit_endpoint(
            [point(0, first_xy), point(1, second_xy)],
            0,
            0.25,
            transformer,
        )

        self.assertAlmostEqual(endpoint.point.x, first_xy[0] + 250.0, places=7)
        self.assertAlmostEqual(endpoint.point.y, first_xy[1] + 100.0, places=7)
        projected = transformer.forward(endpoint.point.lon, endpoint.point.lat)
        self.assertAlmostEqual(projected[0], endpoint.point.x, places=6)
        self.assertAlmostEqual(projected[1], endpoint.point.y, places=6)

    def test_csv_excluded_point_resolves_source_row_without_kept_membership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["vehicle_id", "lon"])
                writer.writeheader()
                writer.writerow({"vehicle_id": "truck-1", "lon": "116.1"})

            store = _MemoryStore()
            store.rows[("excluded_point", 7)] = {
                "id": 7,
                "source_id": str(path),
                "source_row_key": "2",
            }
            trace = ReplayService(
                store,
                source_row_resolver=PostgresSourceRowResolver(conn=None),
            ).replay(ReplayTarget("excluded_point", 7, clean_id="clean-1"))

        self.assertEqual(trace.status, "ok")
        self.assertEqual(
            trace.nodes[0].raw_row,
            {"vehicle_id": "truck-1", "lon": "116.1"},
        )
        self.assertEqual(trace.edges, [])

    def test_source_reference_resolver_reads_named_postgres_table(self) -> None:
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = ({"id": 42, "vehicle_id": "truck-2"},)
        conn = MagicMock()
        conn.cursor.return_value = cursor

        row = PostgresSourceRowResolver(conn)("archive.raw_points", "42")

        cursor.execute.assert_called_once_with(
            "SELECT to_jsonb(t) FROM archive.raw_points AS t WHERE id = %s",
            ("42",),
        )
        self.assertEqual(row, {"id": 42, "vehicle_id": "truck-2"})

    def test_replay_cli_always_shuts_down_logger(self) -> None:
        from track.v2.cli import replay as replay_cli

        config = SimpleNamespace(
            host="localhost", port=5432, database="track", user="postgres",
            password="", connect_timeout=5, sslmode="prefer",
        )
        with (
            patch("sys.argv", [
                "track.v2.cli.replay", "--kind", "trajectory_point", "--id", "1",
            ]),
            patch.object(replay_cli, "default_postgres_config", return_value=config),
            patch.object(replay_cli, "init_logger") as init_logger,
            patch.object(replay_cli, "shutdown_logger") as shutdown_logger,
            patch.object(replay_cli, "pg_connect", side_effect=RuntimeError("offline")),
        ):
            self.assertEqual(replay_cli.main(), 2)

        init_logger.assert_called_once_with()
        shutdown_logger.assert_called_once_with()
    def test_centerline_replay_returns_grouped_current_points_and_bracket_refs(self) -> None:
        class SequenceCursor:
            def __init__(self, conn):
                self.conn = conn
                self.rows = []
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None
            def execute(self, sql, params):
                self.conn.sql.append(sql)
                self.rows = self.conn.responses.pop(0)
            def fetchone(self):
                return self.rows[0] if self.rows else None
            def __iter__(self):
                return iter(self.rows)
        class SequenceConnection:
            def __init__(self, responses):
                self.responses = responses
                self.sql = []
            def cursor(self):
                return SequenceCursor(self)

        point_rows = []
        for index in range(4):
            lon = 100.0 + index
            lat = 30.0
            x, y = _REPLAY_TRANSFORMER.forward(lon, lat)
            point_rows.append((
                "vehicle-1", 2, index, "source", str(index), float(index),
                lon, lat, 90.0, 5.0, {}, None, None, "", x, y,
            ))
        manifest = [
            (0, "clip-1", "vehicle-1", 2, 20, 0, 0.5, 1, 3,
             10, 2, 0.5, True, 4),
        ]
        conn = SequenceConnection([
            [("fit-1", "detect-1", "directed", 10, 20,
              "LINESTRING(0 0,1 1)", 1, 0)],
            manifest,
            [(
                "clean-1", "source", {"target_table": "trajectory_points"},
                "completed", 4326, 3857, _REPLAY_CONTEXT.projection_config,
            )],
            point_rows,
        ])

        result = replay_centerline_inputs(conn, 7)

        self.assertEqual(result.centerline_wkt, "LINESTRING(0 0,1 1)")
        self.assertEqual(len(result.clips), 1)
        clip = result.clips[0]
        self.assertEqual(
            [point.point_ref.source_row_key for point in clip.interior_points],
            ["1", "2"],
        )
        self.assertEqual(
            [ref.source_row_key for ref in clip.start.bracket_refs], ["0", "1"],
        )
        self.assertGreater(clip.points[0].lon, clip.points[-1].lon)
        self.assertIn("centerline_source_clips", conn.sql[1])
        self.assertNotIn("is_active", conn.sql[3])
        self.assertIn("JOIN requested", conn.sql[3])

    def test_detect_to_clean_context_is_cached(self) -> None:
        conn = _LookupConnection()
        store = PostgresReplayStore(conn)
        owner = ReplayTarget("turn_event", 1, detect_id="detect-1")

        self.assertEqual(store._clean_context(owner), ("clean-1", "csv", "trajectory_points"))
        self.assertEqual(store._clean_context(owner), ("clean-1", "csv", "trajectory_points"))

        detect_queries = [
            sql for sql, _ in conn.cursor_.executions
            if sql.startswith("SELECT clean_id FROM detect_runs")
        ]
        self.assertEqual(len(detect_queries), 1)

    def test_cli_target_uses_the_stage_id_required_by_kind(self) -> None:
        parser = MagicMock()
        clean = _target_from_args(
            parser,
            SimpleNamespace(
                kind="kept_trajectory_point", id=7,
                clean_id="clean-1", detect_id=None,
            ),
        )
        detect = _target_from_args(
            parser,
            SimpleNamespace(
                kind="intersection", id=8,
                clean_id=None, detect_id="detect-1",
            ),
        )
        raw = _target_from_args(
            parser,
            SimpleNamespace(
                kind="trajectory_point", id=9,
                clean_id=None, detect_id=None,
            ),
        )
        centerline = _target_from_args(
            parser,
            SimpleNamespace(
                kind="centerline", id=10,
                clean_id=None, detect_id=None,
            ),
        )

        self.assertEqual(clean, ReplayTarget("kept_trajectory_point", 7, clean_id="clean-1"))
        self.assertEqual(detect, ReplayTarget("intersection", 8, detect_id="detect-1"))
        self.assertEqual(raw, ReplayTarget("trajectory_point", 9))
        self.assertEqual(centerline, ReplayTarget("centerline", 10))
        parser.error.assert_not_called()

    def test_postgres_store_filters_clean_and_detect_kinds_by_named_stage_id(self) -> None:
        conn = _LookupConnection()
        store = PostgresReplayStore(conn)

        store.get_entity(ReplayTarget("kept_trajectory_point", 7, clean_id="clean-1"))
        self.assertIn("clean_id = %s", conn.cursor_.sql)
        self.assertEqual(conn.cursor_.params, [7, "clean-1"])

        store.get_entity(ReplayTarget("intersection", 8, detect_id="detect-1"))
        self.assertIn("detect_id = %s", conn.cursor_.sql)
        self.assertEqual(conn.cursor_.params, [8, "detect-1"])

        store.get_entity(ReplayTarget("trajectory_point", 9))
        self.assertNotIn("clean_id", conn.cursor_.sql)
        self.assertNotIn("detect_id", conn.cursor_.sql)
        self.assertEqual(conn.cursor_.params, [9])

    def test_intersection_replays_through_candidate_and_event_to_raw_row(self) -> None:
        raw_row = {"id": "42", "vehicle_id": "truck-1", "lon": "116.1"}
        store = _MemoryStore()
        store.rows.update(
            {
                ("intersection", 30): {"id": 30, "candidate_intersection_id": 20},
                ("candidate_intersection", 20): {"id": 20, "sources": ["input.csv"]},
                ("candidate_intersection_event", 15): {"id": 15, "turn_event_id": 10},
                ("turn_event", 10): {"id": 10, "trajectory_point_id": 1},
                ("candidate_entry_exit_point", 40): {"id": 40},
                ("entry_exit_point", 50): {"id": 50},
                ("trajectory_point", 1): {
                    "id": 1,
                    "source_id": "input.csv",
                    "source_row_key": "42",
                    "source_row_hash": source_row_hash(raw_row),
                },
            }
        )
        store.links.update(
            {
                ("intersection", 30): [
                    ("candidate", ReplayTarget("candidate_intersection", 20, detect_id="detect-1")),
                    ("entry_exit_observation", ReplayTarget("entry_exit_point", 50, detect_id="detect-1")),
                ],
                ("candidate_intersection", 20): [
                    ("member_event", ReplayTarget("candidate_intersection_event", 15, detect_id="detect-1")),
                    ("crossing", ReplayTarget("candidate_entry_exit_point", 40, detect_id="detect-1")),
                ],
                ("candidate_intersection_event", 15): [("turn_event", ReplayTarget("turn_event", 10, detect_id="detect-1"))],
                ("turn_event", 10): [("input_point", ReplayTarget("trajectory_point", 1))],
                ("candidate_entry_exit_point", 40): [("segment_start", ReplayTarget("trajectory_point", 1))],
                ("entry_exit_point", 50): [("entry_crossing", ReplayTarget("candidate_entry_exit_point", 40, detect_id="detect-1"))],
            }
        )

        trace = ReplayService(
            store,
            source_row_resolver=lambda _source_id, _row_key: raw_row,
        ).replay(ReplayTarget("intersection", 30, detect_id="detect-1"))

        self.assertEqual(trace.status, "ok")
        self.assertEqual(
            {node.kind for node in trace.nodes},
            {
                "intersection", "candidate_intersection", "candidate_intersection_event",
                "turn_event", "candidate_entry_exit_point", "entry_exit_point", "trajectory_point",
            },
        )
        leaf = next(node for node in trace.nodes if node.kind == "trajectory_point")
        self.assertEqual(leaf.raw_row, raw_row)
        self.assertEqual(
            {edge.relation for edge in trace.edges},
            {"candidate", "member_event", "turn_event", "input_point", "crossing", "entry_exit_observation", "entry_crossing", "segment_start"},
        )

    def test_changed_source_row_is_reported_instead_of_returned(self) -> None:
        stored_row = {"id": "42", "vehicle_id": "truck-1"}
        changed_row = {"id": "42", "vehicle_id": "other-truck"}
        store = _MemoryStore()
        store.rows[("trajectory_point", 1)] = {
            "id": 1,
            "source_id": "input.csv",
            "source_row_key": "42",
            "source_row_hash": source_row_hash(stored_row),
        }

        trace = ReplayService(
            store,
            source_row_resolver=lambda _source_id, _row_key: changed_row,
        ).replay(ReplayTarget("trajectory_point", 1))

        self.assertEqual(trace.status, "source_mismatch")
        self.assertIsNone(trace.nodes[0].raw_row)
        self.assertEqual(trace.errors[0].code, "source_row_hash_mismatch")

    def test_missing_parent_chain_is_ok_when_no_provenance_columns(self) -> None:
        store = _MemoryStore()
        store.rows[("intersection", 1)] = {"id": 1}

        trace = ReplayService(
            store, source_row_resolver=lambda _source_id, _row_key: {},
        ).replay(ReplayTarget("intersection", 1, detect_id="legacy-detect"))

        # PG-only mode: no provenance columns on trajectory_points,
        # so missing parent chain is not an error.
        self.assertEqual(trace.status, "ok")

    def test_entry_exit_preserves_both_segment_endpoints_and_same_row_keys(self) -> None:
        raw_rows = {
            ("csv-a", "8"): {"id": "a8"},
            ("csv-a", "9"): {"id": "a9"},
            ("csv-b", "8"): {"id": "b8"},
        }
        store = _MemoryStore()
        for db_id, (source_id, row_key) in enumerate(raw_rows, start=1):
            store.rows[("trajectory_point", db_id)] = {
                "id": db_id,
                "source_id": source_id,
                "source_row_key": row_key,
                "source_row_hash": source_row_hash(raw_rows[(source_id, row_key)]),
            }
        store.rows.update({
            ("entry_exit_point", 30): {"id": 30},
            ("candidate_entry_exit_point", 20): {"id": 20},
            ("candidate_entry_exit_point", 21): {"id": 21},
        })
        store.links.update({
            ("entry_exit_point", 30): [
                ("entry_crossing", ReplayTarget("candidate_entry_exit_point", 20, detect_id="detect-1")),
                ("exit_crossing", ReplayTarget("candidate_entry_exit_point", 21, detect_id="detect-1")),
            ],
            ("candidate_entry_exit_point", 20): [
                ("segment_start", ReplayTarget("trajectory_point", 1)),
                ("segment_end", ReplayTarget("trajectory_point", 2)),
            ],
            ("candidate_entry_exit_point", 21): [
                ("segment_start", ReplayTarget("trajectory_point", 3)),
            ],
        })

        trace = ReplayService(
            store,
            source_row_resolver=lambda source_id, row_key: raw_rows[(source_id, row_key)],
        ).replay(ReplayTarget("entry_exit_point", 30, detect_id="detect-1"))

        self.assertEqual(trace.status, "ok")
        self.assertEqual(
            {(node.data.get("source_id"), node.data.get("source_row_key"))
             for node in trace.nodes if node.kind == "trajectory_point"},
            {("csv-a", "8"), ("csv-a", "9"), ("csv-b", "8")},
        )

    def test_postgres_store_expands_complete_intersection_and_typed_source_pairs(self) -> None:
        store = PostgresReplayStore(_LookupConnection())
        target = ReplayTarget("intersection", 30, detect_id="detect-1")

        intersection_parents = store.parent_refs(
            target, {"candidate_intersection_id": 20},
        )
        candidate_parents = store.parent_refs(
            ReplayTarget("candidate_intersection", 20, detect_id="detect-1"), {},
        )
        self.assertEqual(store._conn.cursor_.params, (20, "detect-1"))
        member_parents = store.parent_refs(
            ReplayTarget("candidate_intersection_event", 15, detect_id="detect-1"),
            {
                "turn_event_id": None,
                "from_source_id": "csv-a",
                "from_source_row_key": "1",
                "to_source_id": "csv-b",
                "to_source_row_key": "2",
            },
        )

        self.assertEqual(
            {(relation, parent.kind) for relation, parent in intersection_parents},
            {("candidate", "candidate_intersection"), ("entry_exit_observation", "entry_exit_point")},
        )
        self.assertEqual(
            {(relation, parent.kind) for relation, parent in candidate_parents},
            {("member_event", "candidate_intersection_event"), ("crossing", "candidate_entry_exit_point")},
        )
        self.assertEqual(
            {(parent.kind, parent.id) for _, parent in member_parents},
            {("trajectory_point", 1), ("trajectory_point", 2)},
        )

    def test_every_persisted_kind_reaches_a_verified_raw_row(self) -> None:
        raw_rows = {1: {"id": "1"}, 2: {"id": "2"}}
        store = _MemoryStore()
        for raw_id, raw_row in raw_rows.items():
            store.rows[("trajectory_point", raw_id)] = {
                "id": raw_id,
                "source_id": "input.csv",
                "source_row_key": str(raw_id),
                "source_row_hash": source_row_hash(raw_row),
            }
        kinds = [
            "excluded_point", "kept_trajectory_point", "injected_point", "tracks_cross",
            "turn_event", "candidate_intersection_event", "candidate_entry_exit_point",
            "candidate_intersection", "intersection", "entry_exit_point",
        ]
        for db_id, kind in enumerate(kinds, start=10):
            store.rows[(kind, db_id)] = {"id": db_id}
            store.links[(kind, db_id)] = [("raw", ReplayTarget("trajectory_point", 1))]
        store.rows[("candidate_intersection", 17)] = {"id": 17}
        store.links[("candidate_intersection", 17)] = [
            ("member_event", ReplayTarget("candidate_intersection_event", 15, detect_id="detect-1")),
        ]
        store.rows[("intersection", 18)] = {"id": 18}
        store.links[("intersection", 18)] = [
            ("candidate", ReplayTarget("candidate_intersection", 17, detect_id="detect-1")),
        ]
        store.rows[("entry_exit_point", 19)] = {"id": 19}
        store.links[("entry_exit_point", 19)] = [
            ("entry_crossing", ReplayTarget("candidate_entry_exit_point", 16, detect_id="detect-1")),
        ]

        service = ReplayService(
            store,
            source_row_resolver=lambda _source_id, row_key: raw_rows[int(row_key)],
        )
        for kind in ["trajectory_point", *kinds]:
            db_id = 1 if kind == "trajectory_point" else 10 + kinds.index(kind)
            trace = service.replay(ReplayTarget(kind, db_id, clean_id="clean-1" if kind in {"excluded_point", "kept_trajectory_point", "injected_point"} else None, detect_id="detect-1" if kind not in {"trajectory_point", "excluded_point", "kept_trajectory_point", "injected_point"} else None))
            self.assertEqual(trace.status, "ok", kind)


if __name__ == "__main__":
    unittest.main()
