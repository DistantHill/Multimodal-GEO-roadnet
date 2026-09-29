from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch
import numpy as np
from track.v2.clips.clip_input import ClipRelation
from track.v2.projection import CoordinateTransformer

from track.v2 import fit_centerline_variants
from track.v2.db.write_fitting_results import insert_centerline_bundle
from track.v2.models import (
    ClipReport,
    ClipResult,
    SpatialContext,
    TrackClip,
)
from .config_factory import fitting_config, runtime_config


_SPATIAL_CONTEXT = SpatialContext(
    source_srid=4326,
    processing_srid=3857,
    projection_config={"coordinate_semantics_version": 2, "enforce_area_of_use": True, "max_scale_error": 0.01},
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


def _clip(clip_id: str, start_id: int, end_id: int, reverse: bool) -> TrackClip:
    start_lon, end_lon = ((2.,0.) if reverse else (0.,2.))
    lonlat = np.asarray([(start_lon,0.), (1.,0.), (end_lon,0.)], dtype=np.float64)
    return TrackClip(
        clip_id=clip_id, vehicle_id=f"vehicle-{clip_id}", trip_id=f"trip-{clip_id}",
        clip_type="between_intersection", xy=CoordinateTransformer(_SPATIAL_CONTEXT).forward_many(lonlat),
        lonlat=lonlat, resolved_point_ids=np.asarray([2000+start_id], dtype=np.int64),
        start_index=0,end_index=2,interior_start=1,interior_end=2,
        start_kind="exit",end_kind="entry",start_intersection_id=start_id,end_intersection_id=end_id,
        start_crossing_id=1000+start_id,end_crossing_id=1000+end_id,
        distance_m=200.,point_count=3,subtrack_index=3,clip_ordinal_in_track=4,
    )


def _report(*, vehicles: int, clips: int, points: int) -> ClipReport:
    return ClipReport(
        total_vehicles=vehicles,
        total_subtracks=vehicles,
        total_clips=clips,
        between_intersection_clips=clips,
        boundary_start_clips=0,
        boundary_end_clips=0,
        full_track_clips=0,
        total_points_in_clips=points,
    )


class FitPersistenceTest(unittest.TestCase):
    def test_duckdb_manifest_reads_metadata_without_decoding_source_points(self) -> None:
        source_clips = (
            _clip("forward", 10, 20, False),
            _clip("reverse", 20, 10, True),
        )
        tuple_result = ClipResult(
            clips=source_clips,
            report=_report(vehicles=2, clips=2, points=6),
            spatial_context=_SPATIAL_CONTEXT,
            detect_id="detect-1",
        )
        bundle = fit_centerline_variants(tuple_result, config=fitting_config())
        expected = _Connection()
        insert_centerline_bundle(
            expected, bundle, tuple_result, fit_id="fit-1", detect_id="detect-1",
        )
        with TemporaryDirectory() as data_root:
            with ClipRelation(runtime_config(), data_root, batch_size=2) as relation:
                relation.append_batch(source_clips)
                relation.seal()
                streamed = ClipResult(
                    clips=relation, report=tuple_result.report,
                    spatial_context=_SPATIAL_CONTEXT, detect_id="detect-1",
                )
                actual = _Connection()
                with patch.object(relation, "_read_point_batch", side_effect=AssertionError(
                    "source points decoded during manifest write",
                )):
                    insert_centerline_bundle(
                        actual, bundle, streamed, fit_id="fit-1", detect_id="detect-1",
                    )
                self.assertEqual(actual.copied_rows, expected.copied_rows)

    def test_bundle_persists_three_lines_and_ordered_clip_manifests(self) -> None:
        clips = ClipResult(
            clips=(
                _clip("forward", 10, 20, False),
                _clip("reverse", 20, 10, True),
            ),
            report=_report(vehicles=2, clips=2, points=6),
            spatial_context=_SPATIAL_CONTEXT,
            detect_id="detect-1",
        )
        bundle = fit_centerline_variants(clips, config=fitting_config())
        conn = _Connection()

        ids = insert_centerline_bundle(
            conn, bundle, clips, fit_id="fit-1", detect_id="detect-1",
        )

        self.assertEqual(ids, (100, 101, 102))
        self.assertEqual((conn.commit_count, conn.rollback_count), (0, 0))
        centerline_inserts = [
            (sql, params)
            for sql, params in conn.executions
            if "INSERT INTO centerlines" in sql
        ]
        self.assertEqual(len(centerline_inserts), 1)
        centerline_sql, centerline_params = centerline_inserts[0]
        self.assertIn("from_intersection_id", centerline_sql)
        self.assertIn("to_intersection_id", centerline_sql)
        self.assertNotIn("cluster_id", centerline_sql)
        self.assertEqual(
            [
                (
                    centerline_params[offset + 2],
                    centerline_params[offset + 3],
                    centerline_params[offset + 4],
                    centerline_params[offset + 7],
                    centerline_params[offset + 8],
                )
                for offset in range(0, len(centerline_params), 9)
            ],
            [
                ("combined", 10, 20, 1, 1),
                ("directed", 10, 20, 1, 0),
                ("directed", 20, 10, 1, 0),
            ],
        )
        manifest_copies = [
            sql for sql, _ in conn.executions
            if "COPY centerline_source_clips" in sql
        ]
        self.assertEqual(len(manifest_copies), 1)
        self.assertIn("start_crossing_id", manifest_copies[0])
        self.assertIn("end_crossing_id", manifest_copies[0])
        self.assertNotIn("cluster_id", manifest_copies[0])
        combined = conn.copied_rows
        self.assertEqual(combined[0], (100, "detect-1", 0, 1010, 1020, False))
        self.assertEqual(combined[1], (100, "detect-1", 1, 1020, 1010, True))
        persisted_counts = {
            100 + row_index: (
                centerline_params[row_index * 9 + 7],
                centerline_params[row_index * 9 + 8],
            )
            for row_index in range(3)
        }
        manifest_counts = {
            centerline_id: (
                sum(
                    row[0] == centerline_id and not row[5]
                    for row in conn.copied_rows
                ),
                sum(
                    row[0] == centerline_id and bool(row[5])
                    for row in conn.copied_rows
                ),
            )
            for centerline_id in persisted_counts
        }
        self.assertEqual(persisted_counts, manifest_counts)

    def test_returned_rows_are_mapped_to_bundle_order_by_stable_key(self) -> None:
        clips = ClipResult(
            clips=(
                _clip("forward", 10, 20, False),
                _clip("reverse", 20, 10, True),
            ),
            report=_report(vehicles=2, clips=2, points=6),
            spatial_context=_SPATIAL_CONTEXT,
            detect_id="detect-1",
        )
        conn = _Connection(reverse_returned=True)

        ids = insert_centerline_bundle(
            conn,
            fit_centerline_variants(clips, config=fitting_config()),
            clips,
            fit_id="fit-1",
            detect_id="detect-1",
        )

        self.assertEqual(ids, (100, 101, 102))
        self.assertEqual([row[0] for row in conn.copied_rows], [100, 100, 101, 102])

    def test_incomplete_return_mapping_aborts_before_manifest_insert(self) -> None:
        clips = ClipResult(
            clips=(_clip("forward", 10, 20, False),),
            report=_report(vehicles=1, clips=1, points=3),
            spatial_context=_SPATIAL_CONTEXT,
            detect_id="detect-1",
        )
        conn = _Connection(drop_last_returned=True)

        with self.assertRaisesRegex(RuntimeError, "incomplete id mapping"):
            insert_centerline_bundle(
                conn,
                fit_centerline_variants(clips, config=fitting_config()),
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
