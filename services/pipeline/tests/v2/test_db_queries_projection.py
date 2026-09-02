"""Run-scoped projection contracts owned by the database query boundary."""

from __future__ import annotations

import unittest

from track.v2.db.queries import (
    SpatialWindow,
    SpatialWindowIterator,
    _query_active_points_in_window,
    _query_spatial_extent,
    build_active_in_window_sql,
    build_spatial_extent_sql,
    fetch_detect_row,
)
from track.v2.models import SpatialContext
from track.v2.projection import CoordinateTransformer, ProjectionError


_SPATIAL_CONTEXT = SpatialContext(
    source_srid=4326,
    processing_srid=32650,
    projection_config={"coordinate_semantics_version": 2, "enforce_area_of_use": True, "max_scale_error": 0.01},
)


class _Cursor:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row
        self.sql = ""
        self.params: tuple[object, ...] = ()

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.sql = sql
        self.params = params

    def fetchone(self) -> tuple[object, ...] | None:
        return self.row

    def __iter__(self):
        return iter(() if self.row is None else (self.row,))


class _Connection:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.cursor_instance = _Cursor(row)

    def cursor(self) -> _Cursor:
        return self.cursor_instance


class ProjectedSpatialQueryTest(unittest.TestCase):
    def test_kept_extent_includes_injected_points_in_same_metric_space(self) -> None:
        sql = build_spatial_extent_sql(
            "raw_points",
            kept_table="kept_trajectory_points",
            spatial_context=_SPATIAL_CONTEXT,
        )

        self.assertIn("ST_Transform(tp.geom, 32650)", sql)
        self.assertIn("UNION ALL", sql)
        self.assertIn("SELECT ip.x, ip.y FROM injected_points AS ip", sql)
        self.assertEqual(sql.count("%s"), 2)
        self.assertNotIn("geom_projected", sql)

    def test_extent_executor_binds_clean_id_to_kept_and_injected_branches(self) -> None:
        conn = _Connection((100.0, 900.0, 200.0, 1000.0))

        extent = _query_spatial_extent(
            conn,
            table="raw_points",
            kept_table="kept_trajectory_points",
            clean_id="clean-1",
            spatial_context=_SPATIAL_CONTEXT,
        )

        self.assertEqual(extent, (100.0, 200.0, 900.0, 1000.0))
        self.assertEqual(conn.cursor_instance.params, ("clean-1", "clean-1"))

    def test_window_query_uses_raw_and_injected_gist_prefilters(self) -> None:
        sql = build_active_in_window_sql(
            "raw_points",
            has_clean_id=True,
            kept_table="kept_trajectory_points",
            spatial_context=_SPATIAL_CONTEXT,
        )

        self.assertIn(
            "AND ST_Transform(tp.geom, 32650) && "
            "ST_MakeEnvelope(%s, %s, %s, %s, 32650)",
            sql,
        )
        self.assertIn(
            "AND tp.geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)",
            sql,
        )
        self.assertIn(
            "WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)",
            sql,
        )
        self.assertIn("AND x >= %s AND y >= %s AND x <= %s AND y <= %s", sql)
        self.assertEqual(sql.count("ST_MakeEnvelope(%s, %s, %s, %s, 4326)"), 2)
        self.assertEqual(sql.count("%s"), 19)
        self.assertIn("from_source_row_key, to_source_row_key", sql)
        self.assertIn("ST_X(ST_Transform(tp.geom, 32650)) AS x", sql)
        self.assertNotIn("geom_projected", sql)

    def test_raw_query_without_clean_id_excludes_unscoped_injected_xy(self) -> None:
        sql = build_active_in_window_sql(
            "raw_points",
            has_clean_id=False,
            spatial_context=_SPATIAL_CONTEXT,
        )

        self.assertNotIn("injected_points", sql)
        self.assertNotIn("UNION ALL", sql)
        self.assertIn("geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)", sql)
        self.assertEqual(sql.count("%s"), 9)

    def test_raw_mode_with_clean_id_is_rejected_as_incomplete_run_isolation(self) -> None:
        conn = _Connection((100.0, 900.0, 200.0, 1000.0))

        with self.assertRaisesRegex(ValueError, "requires kept_table"):
            _query_spatial_extent(
                conn,
                table="raw_points",
                clean_id="clean-1",
                spatial_context=_SPATIAL_CONTEXT,
            )

    def test_window_executor_binds_densified_source_prefilter_before_exact_bounds(self) -> None:
        conn = _Connection(None)
        window = SpatialWindow(400000.0, 4300000.0, 402000.0, 4302000.0)
        expected_source = CoordinateTransformer(_SPATIAL_CONTEXT).inverse_bounds(
            (window.x_min, window.y_min, window.x_max, window.y_max)
        )

        points = _query_active_points_in_window(
            conn,
            window,
            table="raw_points",
            source_id="source-1",
            spatial_context=_SPATIAL_CONTEXT,
        )

        self.assertEqual(points, [])
        self.assertEqual(
            conn.cursor_instance.params,
            ["source-1", *expected_source, 400000.0, 4300000.0, 402000.0, 4302000.0],
        )

    def test_kept_window_reuses_source_prefilter_for_injected_geometry(self) -> None:
        conn = _Connection(None)
        window = SpatialWindow(400000.0, 4300000.0, 402000.0, 4302000.0)
        expected_source = CoordinateTransformer(_SPATIAL_CONTEXT).inverse_bounds(
            (window.x_min, window.y_min, window.x_max, window.y_max)
        )
        projected_bounds = (
            window.x_min,
            window.y_min,
            window.x_max,
            window.y_max,
        )

        points = _query_active_points_in_window(
            conn,
            window,
            table="raw_points",
            kept_table="kept_trajectory_points",
            clean_id="clean-1",
            spatial_context=_SPATIAL_CONTEXT,
        )

        self.assertEqual(points, [])
        self.assertEqual(
            conn.cursor_instance.params,
            [
                "clean-1",
                *expected_source,
                *projected_bounds,
                "public.injected_points",
                *expected_source,
                *projected_bounds,
                "clean-1",
            ],
        )

    def test_window_iterator_uses_exact_processing_crs_metres(self) -> None:
        iterator = SpatialWindowIterator(
            (400000.0, 4300000.0, 405000.0, 4303000.0),
            size=(2000.0, 1000.0),
            overlap=0.0,
        )

        windows = list(iterator.iter_windows())

        self.assertEqual(iterator.count_windows(), 9)
        self.assertEqual(
            (windows[0].x_min, windows[0].y_min, windows[0].x_max, windows[0].y_max),
            (400000.0, 4300000.0, 402000.0, 4301000.0),
        )


class DetectRunSpatialContextQueryTest(unittest.TestCase):
    def test_detect_run_inherits_validated_clean_run_context(self) -> None:
        conn = _Connection((
            "clean-1",
            "raw_points",
            {"target_table": "raw_points"},
            "completed",
            4326,
            32650,
            _SPATIAL_CONTEXT.projection_config,
        ))

        row = fetch_detect_row(conn, "detect-1")

        self.assertEqual(row.spatial_context, _SPATIAL_CONTEXT)
        self.assertIn("JOIN clean_runs AS c", conn.cursor_instance.sql)
        self.assertEqual(conn.cursor_instance.params, ("detect-1",))

    def test_detect_run_rejects_legacy_null_processing_srid(self) -> None:
        conn = _Connection((
            "clean-legacy", "raw_points", {}, "completed", 4326, None, {},
        ))

        with self.assertRaisesRegex(ProjectionError, "no processing_srid"):
            fetch_detect_row(conn, "detect-legacy")


if __name__ == "__main__":
    unittest.main()
