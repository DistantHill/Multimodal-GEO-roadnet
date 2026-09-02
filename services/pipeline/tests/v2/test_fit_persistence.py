from __future__ import annotations

import unittest

from track.v2 import fit_centerline_variants
from track.v2.db_writer import insert_centerline_bundle
from track.v2.models import (
    ClipEndpoint,
    ClipReport,
    ClipResult,
    PointRef,
    TrackClip,
    TrajectoryPoint,
)


class _Cursor:
    def __init__(self, conn: "_Connection") -> None:
        self.conn = conn
        self.rows: list[tuple[object, ...]] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: object = ()) -> None:
        self.conn.executions.append((" ".join(sql.split()), params))
        if "INSERT INTO centerlines" in sql:
            values = list(params)
            self.rows = []
            for offset in range(0, len(values), 9):
                row = values[offset : offset + 9]
                self.rows.append((
                    self.conn.next_centerline_id,
                    row[2],
                    row[3],
                    row[4],
                ))
                self.conn.next_centerline_id += 1

    def fetchone(self) -> tuple[object, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        if self.conn.drop_last_returned:
            return self.rows[:-1]
        if self.conn.reverse_returned:
            return list(reversed(self.rows))
        return self.rows

    def copy(self, sql: str) -> "_Copy":
        self.conn.executions.append((" ".join(sql.split()), ()))
        return _Copy(self.conn)


class _Copy:
    def __init__(self, conn: "_Connection") -> None:
        self.conn = conn

    def __enter__(self) -> "_Copy":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def write_row(self, row: tuple[object, ...]) -> None:
        self.conn.copied_rows.append(row)


class _Connection:
    def __init__(
        self,
        *,
        reverse_returned: bool = False,
        drop_last_returned: bool = False,
    ) -> None:
        self.executions: list[tuple[str, object]] = []
        self.next_centerline_id = 100
        self.reverse_returned = reverse_returned
        self.drop_last_returned = drop_last_returned
        self.commit_count = 0
        self.rollback_count = 0
        self.copied_rows: list[tuple[object, ...]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


def _point(vehicle: str, key: str, lon: float) -> TrajectoryPoint:
    return TrajectoryPoint(
        PointRef("source", key, True), vehicle, lon, lon, 0.0, 90.0, 5.0,
    )


def _clip(clip_id: str, start_id: int, end_id: int, reverse: bool) -> TrackClip:
    vehicle = f"vehicle-{clip_id}"
    start_lon, end_lon = ((2.0, 0.0) if reverse else (0.0, 2.0))
    return TrackClip(
        clip_id=clip_id,
        vehicle_id=vehicle,
        trip_id=f"trip-{clip_id}",
        clip_type="inter_intersection",
        start=ClipEndpoint("exit", start_lon, 0.0, 0.0, start_id, 0, 0.25),
        end=ClipEndpoint("entry", end_lon, 0.0, 2.0, end_id, 2, 0.75),
        points=(_point(vehicle, f"{clip_id}-1", 1.0),),
        distance_m=200.0,
        point_count=3,
        subtrack_index=3,
        clip_ordinal_in_track=4,
        interior_start_index=1,
        interior_end_index_exclusive=2,
    )


class FitPersistenceTest(unittest.TestCase):
    def test_bundle_persists_three_lines_and_ordered_clip_manifests(self) -> None:
        clips = ClipResult(
            clips=(
                _clip("forward", 10, 20, False),
                _clip("reverse", 20, 10, True),
            ),
            report=ClipReport(2, 2, 2, 0, 6),
            detect_id="detect-1",
        )
        bundle = fit_centerline_variants(clips)
        conn = _Connection()

        ids = insert_centerline_bundle(
            conn, bundle, clips, fit_id="fit-1", detect_id="detect-1",
        )

        self.assertEqual(ids, (100, 101, 102))
        self.assertEqual((conn.commit_count, conn.rollback_count), (0, 0))
        centerline_inserts = [
            params for sql, params in conn.executions if "INSERT INTO centerlines" in sql
        ]
        self.assertEqual(len(centerline_inserts), 1)
        centerline_params = centerline_inserts[0]
        self.assertEqual(
            [
                (
                    centerline_params[offset + 2],
                    centerline_params[offset + 3],
                    centerline_params[offset + 4],
                )
                for offset in range(0, len(centerline_params), 9)
            ],
            [("combined", 10, 20), ("directed", 10, 20), ("directed", 20, 10)],
        )
        manifest_copies = [
            sql for sql, _ in conn.executions
            if "COPY centerline_source_clips" in sql
        ]
        self.assertEqual(len(manifest_copies), 1)
        combined = conn.copied_rows
        self.assertEqual(combined[0][0:4], (100, 0, "forward", "vehicle-forward"))
        self.assertFalse(combined[0][14])
        self.assertEqual(combined[1][0:4], (100, 1, "reverse", "vehicle-reverse"))
        self.assertTrue(combined[1][14])

    def test_returned_rows_are_mapped_to_bundle_order_by_stable_key(self) -> None:
        clips = ClipResult(
            clips=(
                _clip("forward", 10, 20, False),
                _clip("reverse", 20, 10, True),
            ),
            report=ClipReport(2, 2, 2, 0, 6),
            detect_id="detect-1",
        )
        conn = _Connection(reverse_returned=True)

        ids = insert_centerline_bundle(
            conn,
            fit_centerline_variants(clips),
            clips,
            fit_id="fit-1",
            detect_id="detect-1",
        )

        self.assertEqual(ids, (100, 101, 102))
        self.assertEqual([row[0] for row in conn.copied_rows], [100, 100, 101, 102])

    def test_incomplete_return_mapping_aborts_before_manifest_insert(self) -> None:
        clips = ClipResult(
            clips=(_clip("forward", 10, 20, False),),
            report=ClipReport(1, 1, 1, 0, 3),
            detect_id="detect-1",
        )
        conn = _Connection(drop_last_returned=True)

        with self.assertRaisesRegex(RuntimeError, "incomplete id mapping"):
            insert_centerline_bundle(
                conn,
                fit_centerline_variants(clips),
                clips,
                fit_id="fit-1",
                detect_id="detect-1",
            )

        self.assertFalse(any(
            "COPY centerline_source_clips" in sql
            for sql, _ in conn.executions
        ))
        self.assertEqual((conn.commit_count, conn.rollback_count), (0, 0))


if __name__ == "__main__":
    unittest.main()
