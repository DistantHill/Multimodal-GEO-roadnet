from __future__ import annotations

import unittest

from track.v2 import fit_centerline_variants
from track.v2.db_writer import insert_centerline_bundle
from track.v2.models import (
    ClipEndpoint,
    ClipReport,
    ClipResult,
    PointRef,
    SpatialContext,
    TrackClip,
    TrajectoryPoint,
)


_SPATIAL_CONTEXT = SpatialContext(
    source_srid=4326,
    processing_srid=3857,
    projection_config={"coordinate_semantics_version": 2, "enforce_area_of_use": True, "max_scale_error": 0.01},
)


class _Cursor:
    def __init__(self, conn: "_Connection") -> None:
        self.conn = conn
        self.row: tuple[object, ...] | None = None

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: object = ()) -> None:
        self.conn.executions.append((" ".join(sql.split()), params))
        if "INSERT INTO centerlines" in sql:
            self.row = (self.conn.next_centerline_id,)
            self.conn.next_centerline_id += 1

    def fetchone(self) -> tuple[object, ...] | None:
        return self.row


class _Connection:
    def __init__(self) -> None:
        self.executions: list[tuple[str, object]] = []
        self.next_centerline_id = 100

    def cursor(self) -> _Cursor:
        return _Cursor(self)


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
            spatial_context=_SPATIAL_CONTEXT,
            detect_id="detect-1",
        )
        bundle = fit_centerline_variants(clips)
        conn = _Connection()

        ids = insert_centerline_bundle(
            conn, bundle, clips, fit_id="fit-1", detect_id="detect-1",
        )

        self.assertEqual(ids, (100, 101, 102))
        centerline_inserts = [
            params for sql, params in conn.executions if "INSERT INTO centerlines" in sql
        ]
        self.assertEqual(
            [(params[2], params[3], params[4]) for params in centerline_inserts],
            [("combined", 10, 20), ("directed", 10, 20), ("directed", 20, 10)],
        )
        manifest_inserts = [
            params for sql, params in conn.executions
            if "INSERT INTO centerline_source_clips" in sql
        ]
        self.assertEqual(len(manifest_inserts), 3)
        combined = manifest_inserts[0]
        self.assertEqual(combined[0:4], [100, 0, "forward", "vehicle-forward"])
        self.assertFalse(combined[14])
        self.assertEqual(combined[16:20], [100, 1, "reverse", "vehicle-reverse"])
        self.assertTrue(combined[30])


if __name__ == "__main__":
    unittest.main()
