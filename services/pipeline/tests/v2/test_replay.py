"""Public replay-service behaviour tests."""

from __future__ import annotations

import csv
import os
import tempfile
import time
import unittest
from pathlib import Path

from track.v2.replay import (
    PostgresReplayStore,
    PostgresSourceRowResolver,
    ReplayService,
    ReplayTarget,
)
from track.v2.sources import source_row_hash


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

    def __enter__(self) -> _LookupCursor:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.sql = sql
        self.params = params

    def fetchall(self) -> list[tuple[int]]:
        if "candidate_intersection_events" in self.sql:
            return [(15,)]
        if "candidate_entry_exit_points" in self.sql:
            return [(40,)]
        if "entry_exit_points" in self.sql:
            return [(50,)]
        return []

    def fetchone(self) -> tuple[int] | None:
        if "trajectory_points" in self.sql:
            source_id, row_key = self.params
            return {("csv-a", "8"): (1,), ("csv-b", "8"): (2,)}.get((source_id, row_key))
        return None


class _LookupConnection:
    def __init__(self) -> None:
        self.cursor_ = _LookupCursor()

    def cursor(self) -> _LookupCursor:
        return self.cursor_


class ReplayServiceTest(unittest.TestCase):
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
                    ("candidate", ReplayTarget("candidate_intersection", 20, "run-1")),
                    ("entry_exit_observation", ReplayTarget("entry_exit_point", 50, "run-1")),
                ],
                ("candidate_intersection", 20): [
                    ("member_event", ReplayTarget("candidate_intersection_event", 15, "run-1")),
                    ("crossing", ReplayTarget("candidate_entry_exit_point", 40, "run-1")),
                ],
                ("candidate_intersection_event", 15): [("turn_event", ReplayTarget("turn_event", 10, "run-1"))],
                ("turn_event", 10): [("input_point", ReplayTarget("trajectory_point", 1, None))],
                ("candidate_entry_exit_point", 40): [("segment_start", ReplayTarget("trajectory_point", 1, None))],
                ("entry_exit_point", 50): [("entry_crossing", ReplayTarget("candidate_entry_exit_point", 40, "run-1"))],
            }
        )

        trace = ReplayService(
            store,
            source_row_resolver=lambda _source_id, _row_key: raw_row,
        ).replay(ReplayTarget("intersection", 30, "run-1"))

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
        ).replay(ReplayTarget("trajectory_point", 1, None))

        self.assertEqual(trace.status, "source_mismatch")
        self.assertIsNone(trace.nodes[0].raw_row)
        self.assertEqual(trace.errors[0].code, "source_row_hash_mismatch")

    def test_missing_legacy_parent_chain_is_explicitly_reported(self) -> None:
        store = _MemoryStore()
        store.rows[("intersection", 1)] = {"id": 1}

        trace = ReplayService(
            store, source_row_resolver=lambda _source_id, _row_key: {},
        ).replay(ReplayTarget("intersection", 1, "legacy-run"))

        self.assertEqual(trace.status, "legacy_provenance_incomplete")
        self.assertEqual(trace.errors[0].code, "legacy_provenance_incomplete")

    def test_csv_resolver_invalidates_cache_when_source_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["id", "vehicle_id"])
                writer.writeheader()
                writer.writerow({"id": "1", "vehicle_id": "truck"})
            resolver = PostgresSourceRowResolver(conn=None)

            self.assertEqual(resolver(str(path), "2"), {"id": "1", "vehicle_id": "truck"})
            path.write_text("id,vehicle_id\n2,other\n", encoding="utf-8")
            future = time.time() + 1
            os.utime(path, (future, future))
            self.assertEqual(resolver(str(path), "2"), {"id": "2", "vehicle_id": "other"})

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
                ("entry_crossing", ReplayTarget("candidate_entry_exit_point", 20, "run-1")),
                ("exit_crossing", ReplayTarget("candidate_entry_exit_point", 21, "run-1")),
            ],
            ("candidate_entry_exit_point", 20): [
                ("segment_start", ReplayTarget("trajectory_point", 1, None)),
                ("segment_end", ReplayTarget("trajectory_point", 2, None)),
            ],
            ("candidate_entry_exit_point", 21): [
                ("segment_start", ReplayTarget("trajectory_point", 3, None)),
            ],
        })

        trace = ReplayService(
            store,
            source_row_resolver=lambda source_id, row_key: raw_rows[(source_id, row_key)],
        ).replay(ReplayTarget("entry_exit_point", 30, "run-1"))

        self.assertEqual(trace.status, "ok")
        self.assertEqual(
            {(node.data.get("source_id"), node.data.get("source_row_key"))
             for node in trace.nodes if node.kind == "trajectory_point"},
            {("csv-a", "8"), ("csv-a", "9"), ("csv-b", "8")},
        )

    def test_postgres_store_expands_complete_intersection_and_typed_source_pairs(self) -> None:
        store = PostgresReplayStore(_LookupConnection())
        target = ReplayTarget("intersection", 30, "run-1")

        intersection_parents = store.parent_refs(
            target, {"candidate_intersection_id": 20},
        )
        candidate_parents = store.parent_refs(
            ReplayTarget("candidate_intersection", 20, "run-1"), {},
        )
        member_parents = store.parent_refs(
            ReplayTarget("candidate_intersection_event", 15, "run-1"),
            {
                "turn_event_id": None,
                "from_source_id": "csv-a",
                "from_source_row_key": "8",
                "to_source_id": "csv-b",
                "to_source_row_key": "8",
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
            store.links[(kind, db_id)] = [("raw", ReplayTarget("trajectory_point", 1, None))]
        store.rows[("candidate_intersection", 17)] = {"id": 17}
        store.links[("candidate_intersection", 17)] = [
            ("member_event", ReplayTarget("candidate_intersection_event", 15, "run-1")),
        ]
        store.rows[("intersection", 18)] = {"id": 18}
        store.links[("intersection", 18)] = [
            ("candidate", ReplayTarget("candidate_intersection", 17, "run-1")),
        ]
        store.rows[("entry_exit_point", 19)] = {"id": 19}
        store.links[("entry_exit_point", 19)] = [
            ("entry_crossing", ReplayTarget("candidate_entry_exit_point", 16, "run-1")),
        ]

        service = ReplayService(
            store,
            source_row_resolver=lambda _source_id, row_key: raw_rows[int(row_key)],
        )
        for kind in ["trajectory_point", *kinds]:
            db_id = 1 if kind == "trajectory_point" else 10 + kinds.index(kind)
            trace = service.replay(ReplayTarget(kind, db_id, None if kind == "trajectory_point" else "run-1"))
            self.assertEqual(trace.status, "ok", kind)


if __name__ == "__main__":
    unittest.main()
