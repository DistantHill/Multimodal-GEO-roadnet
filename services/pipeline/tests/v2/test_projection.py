from __future__ import annotations

from .config_factory import (clip_config, fitting_config, intersect_config, network_config, postgres_config, projection_config, refine_config, runtime_config)

import unittest

from track.v2.models import ProjectionConfig, SpatialContext
from track.v2.projection import (
    CoordinateTransformer,
    ProjectionError,
    QueryWindowRequest,
    _entered_three_degree_cells,
    _easternmost_utm_srid,
    _query_window_in_srid,
    _utm_srid,
    _validate_query_scale,
    build_query_window,
    create_spatial_context,
    spatial_context_from_values,
)


class SpatialContextTest(unittest.TestCase):
    def test_new_run_requires_explicit_processing_srid(self) -> None:
        with self.assertRaisesRegex(TypeError, "processing_srid"):
            ProjectionConfig(source_srid=4326, enforce_area_of_use=True, max_scale_error=0.01)

    def test_processing_crs_must_be_projected_metres(self) -> None:
        with self.assertRaisesRegex(ProjectionError, "projected CRS"):
            create_spatial_context(projection_config(processing_srid=4326))

    def test_processing_crs_must_have_east_and_north_horizontal_axes(self) -> None:
        # EPSG:2053 uses westing/southing axes. Euclidean distances remain
        # meaningful, but the pipeline's +x-east/+y-north bearing convention
        # would be false even with always_xy=True.
        with self.assertRaisesRegex(ProjectionError, r"\+x east and \+y north"):
            create_spatial_context(projection_config(processing_srid=2053))

    def test_northing_easting_declared_axis_order_is_accepted(self) -> None:
        # always_xy normalizes EPSG:3414's declared northing/easting order.
        context = create_spatial_context(projection_config(processing_srid=3414))

        self.assertEqual(context.processing_srid, 3414)

    def test_known_utm_coordinate_and_round_trip(self) -> None:
        context = create_spatial_context(projection_config(processing_srid=32650))
        transformer = CoordinateTransformer(context)

        x, y = transformer.forward(117.0, 0.0)
        self.assertAlmostEqual(x, 500_000.0, places=6)
        self.assertAlmostEqual(y, 0.0, places=6)
        lon, lat = transformer.inverse(x, y)
        self.assertAlmostEqual(lon, 117.0, places=10)
        self.assertAlmostEqual(lat, 0.0, places=10)

        east_x, east_y = transformer.heading_vector(117.0, 39.0, 90.0)
        self.assertAlmostEqual(east_x, 1.0, places=6)
        self.assertAlmostEqual(east_y, 0.0, places=6)

    def test_utm_area_of_use_is_validated_against_data_bounds(self) -> None:
        context = create_spatial_context(projection_config(processing_srid=32650))
        transformer = CoordinateTransformer(context)
        with self.assertRaisesRegex(ProjectionError, "area of use"):
            transformer.validate_bounds((110.0, 36.0, 116.0, 39.0))

    def test_geographic_bounds_must_be_ordered(self) -> None:
        context = create_spatial_context(projection_config(processing_srid=32650))
        transformer = CoordinateTransformer(context)

        for bounds in (
            (117.1, 39.0, 117.0, 39.1),
            (117.0, 39.1, 117.1, 39.0),
        ):
            with self.subTest(bounds=bounds):
                with self.assertRaisesRegex(ProjectionError, "must be ordered"):
                    transformer.validate_bounds(bounds)

    def test_antimeridian_area_of_use_accepts_either_valid_side(self) -> None:
        context = create_spatial_context(projection_config(
            processing_srid=3338,
            max_scale_error=1.0,
        ))
        transformer = CoordinateTransformer(context)

        transformer.validate_bounds((-150.0, 60.0, -150.0, 60.0))
        transformer.validate_bounds((175.0, 60.0, 175.0, 60.0))
        with self.assertRaisesRegex(ProjectionError, "area of use"):
            transformer.validate_bounds((0.0, 60.0, 0.0, 60.0))

    def test_inverse_bounds_contains_projected_window_corners(self) -> None:
        context = create_spatial_context(projection_config(processing_srid=32650))
        transformer = CoordinateTransformer(context)
        projected = (400000.0, 4300000.0, 402000.0, 4302000.0)

        west, south, east, north = transformer.inverse_bounds(projected)

        for x in (projected[0], projected[2]):
            for y in (projected[1], projected[3]):
                lon, lat = transformer.inverse(x, y)
                self.assertLessEqual(west, lon)
                self.assertLessEqual(south, lat)
                self.assertGreaterEqual(east, lon)
                self.assertGreaterEqual(north, lat)

    def test_inverse_bounds_contains_horizontal_edge_central_meridian_extremum(self) -> None:
        context = create_spatial_context(projection_config(processing_srid=32650))
        transformer = CoordinateTransformer(context)

        bounds = transformer.inverse_bounds((250000.0, 4300000.0, 750000.0, 4400000.0))
        lon, lat = transformer.inverse(500000.0, 4399999.9)

        self.assertLessEqual(bounds[0], lon)
        self.assertGreaterEqual(bounds[3], lat)

    def test_inverse_bounds_rejects_antimeridian_footprint(self) -> None:
        context = create_spatial_context(projection_config(processing_srid=32660))
        transformer = CoordinateTransformer(context)
        with self.assertRaisesRegex(ProjectionError, "antimeridian|local domain"):
            transformer.inverse_bounds((
                701138.6080433005, 4270778.214043551,
                801138.6080433005, 4370778.214043551,
            ))

    def test_inverse_bounds_rejects_far_tm_sheet_coordinates(self) -> None:
        context = create_spatial_context(projection_config(processing_srid=32650))
        transformer = CoordinateTransformer(context)
        with self.assertRaisesRegex(ProjectionError, "local projected domain"):
            transformer.inverse_bounds((
                12322388.235970555, -33228288.425446153,
                12331267.070719512, -28689717.062269293,
            ))
        extreme = CoordinateTransformer(create_spatial_context(projection_config(processing_srid=32660)))
        with self.assertRaisesRegex(ProjectionError, "central-meridian domain"):
            extreme.inverse_bounds((
                -359443.54391068325, 9970379.183979101,
                -356637.8120688874, 9999737.664773617,
            ))

    def test_exact_grid_and_utm_touches_do_not_enter_eastern_cell(self) -> None:
        self.assertEqual(_entered_three_degree_cells(-1.5, 1.5), (0,))
        self.assertEqual(_entered_three_degree_cells(-1.5, 1.5 - 1e-9), (0,))
        self.assertEqual(_entered_three_degree_cells(-1.5, 1.5 + 1e-9), (0, 1))
        self.assertEqual(_easternmost_utm_srid(108.0, 114.0, 39.0), 32649)
        self.assertEqual(_easternmost_utm_srid(108.0, 114.0 - 1e-9, 39.0), 32649)
        self.assertEqual(_easternmost_utm_srid(108.0, 114.0 + 1e-9, 39.0), 32650)
        self.assertEqual(_easternmost_utm_srid(0.0, 6.0, -20.0), 32731)

    def test_query_rejects_invalid_extreme_and_three_cell_requests(self) -> None:
        for request in (
            QueryWindowRequest(float("nan"), 39.0, 1000.0, 1000.0),
            QueryWindowRequest(116.0, 85.0, 1000.0, 1000.0),
            QueryWindowRequest(116.0, 39.0, 0.0, 1000.0),
            QueryWindowRequest(179.5, 39.0, 1000.0, 1000.0),
            QueryWindowRequest(116.0, 0.0, 1000.0, 100000.0),
            QueryWindowRequest(116.0, 39.0, 700000.0, 1000.0),
        ):
            with self.subTest(request=request):
                with self.assertRaises(ProjectionError):
                    build_query_window(request)

    def test_southern_query_uses_southern_standard_utm(self) -> None:
        window = build_query_window(QueryWindowRequest(116.0, -39.0, 1000.0, 1000.0))
        self.assertEqual(window.query_srid, 32750)

    def test_query_window_selects_easternmost_standard_utm(self) -> None:
        window = build_query_window(QueryWindowRequest(113.9, 39.0, 30000.0, 1000.0))

        self.assertEqual(window.query_srid, 32650)
        self.assertEqual(window.request.width_m, window.x_max - window.x_min)
        self.assertEqual(window.request.height_m, window.y_max - window.y_min)

    def test_final_rebuilt_footprint_controls_three_degree_limit(self) -> None:
        request = QueryWindowRequest(112.05, 10.0, 560000.0, 100000.0)
        provisional = _query_window_in_srid(
            request, _utm_srid(request.center_lon, request.center_lat),
            validate_final=False,
        )

        final = build_query_window(request)

        self.assertEqual(len(provisional.three_degree_cells), 3)
        self.assertEqual(final.query_srid, 32650)
        self.assertEqual(len(final.three_degree_cells), 2)

    def test_query_policy_is_independent_from_run_one_percent_policy(self) -> None:
        # 9 degrees from zone 49's central meridian at the equator has about
        # 1.2146% scale error: query policy accepts it, Run default rejects it.
        _validate_query_scale(32649, (120.0, 0.0, 120.0, 0.0))
        run = CoordinateTransformer(create_spatial_context(projection_config(processing_srid=32649)))
        with self.assertRaisesRegex(ProjectionError, "area of use|scale distortion"):
            run.validate_bounds((120.0, 0.0, 120.0, 0.0))

    def test_global_metre_label_does_not_hide_web_mercator_distortion(self) -> None:
        context = create_spatial_context(projection_config(processing_srid=3857))
        transformer = CoordinateTransformer(context)
        with self.assertRaisesRegex(ProjectionError, "scale distortion"):
            transformer.validate_bounds((116.0, 39.0, 116.1, 39.1))

    def test_tissot_principal_scale_catches_oblique_distortion(self) -> None:
        # At this EPSG:5633 location meridional/parallel scales are within 1%,
        # while the Tissot principal scales differ from one by about 3.2%.
        context = create_spatial_context(projection_config(processing_srid=5633))
        transformer = CoordinateTransformer(context)

        with self.assertRaisesRegex(ProjectionError, "scale distortion"):
            transformer.validate_bounds((-24.03, 36.155, -24.03, 36.155))

    def test_legacy_null_processing_srid_is_not_guessed(self) -> None:
        with self.assertRaisesRegex(ProjectionError, "legacy Run"):
            spatial_context_from_values(4326, None, {})

    def test_partially_backfilled_run_without_semantics_marker_is_rejected(self) -> None:
        with self.assertRaisesRegex(ProjectionError, "coordinate_semantics_version=2"):
            spatial_context_from_values(4326, 32650, {})

    def test_persisted_context_is_validated(self) -> None:
        context = spatial_context_from_values(
            4326,
            32650,
            {
                "coordinate_semantics_version": 2,
                "enforce_area_of_use": True,
                "max_scale_error": 0.01,
            },
        )
        self.assertEqual(
            context,
            SpatialContext(
                source_srid=4326,
                processing_srid=32650,
                projection_config={
                    "coordinate_semantics_version": 2,
                    "enforce_area_of_use": True,
                    "max_scale_error": 0.01,
                },
            ),
        )

    def test_json_encoded_projection_config_is_materialized(self) -> None:
        context = spatial_context_from_values(
            4326,
            32650,
            '{"coordinate_semantics_version": 2, '
            '"enforce_area_of_use": false, "max_scale_error": 0.02}',
        )

        self.assertFalse(context.projection_config["enforce_area_of_use"])
        self.assertEqual(context.projection_config["max_scale_error"], 0.02)


if __name__ == "__main__":
    unittest.main()
