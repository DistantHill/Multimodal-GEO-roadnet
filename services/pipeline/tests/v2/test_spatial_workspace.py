"""Regression tests for the DB-mode shared spatial workspace."""

from __future__ import annotations

import math
from unittest.mock import patch

import pytest

from track.v2._geometry import _to_lonlat
from track.v2.cross_track import CrossTrackCapacityError, extract_cross_track_candidates
from track.v2.db.queries import SpatialWindowIterator
from track.v2.detect import (
    IntersectConfig,
    _cross_track_candidate_key,
    _extract_cross_track_db_workspace,
    _extract_cross_track_memory_windows,
    _extract_cross_track_windows,
    _project_tracks,
)
from track.v2.models import PointRef, SpatialContext, TrajectoryPoint
from track.v2.projection import CoordinateTransformer


_SPATIAL_CONTEXT = SpatialContext(
    source_srid=4326,
    processing_srid=32650,
    projection_config={
        "coordinate_semantics_version": 2,
        "enforce_area_of_use": True,
        "max_scale_error": 0.01,
    },
)
_TRANSFORMER = CoordinateTransformer(_SPATIAL_CONTEXT)


class _QuietStatus:
    def update(self, **_kwargs: object) -> None:
        pass


def _point(
    vehicle_id: str,
    key: str,
    timestamp: float,
    lon: float,
    lat: float,
    heading: float,
) -> TrajectoryPoint:
    return TrajectoryPoint(
        PointRef("source", key, stable=True),
        vehicle_id,
        timestamp,
        lon,
        lat,
        heading,
        10.0,
    )


def _crossing_tracks(
    origin_lon: float = 116.0,
    origin_lat: float = 39.0,
) -> dict[str, list[TrajectoryPoint]]:
    a0 = _to_lonlat(0.0, -300.0, origin_lon, origin_lat)
    a1 = _to_lonlat(0.0, 300.0, origin_lon, origin_lat)
    b0 = _to_lonlat(-300.0, 0.0, origin_lon, origin_lat)
    b1 = _to_lonlat(300.0, 0.0, origin_lon, origin_lat)
    tracks = {
        "A": [
            _point("A", "a0", 0.0, *a0, 0.0),
            _point("A", "a1", 1.0, *a1, 0.0),
        ],
        "B": [
            _point("B", "b0", 0.0, *b0, 90.0),
            _point("B", "b1", 1.0, *b1, 90.0),
        ],
    }
    return _project_tracks(tracks, _TRANSFORMER)


def _projected_bounds(
    lon_min: float,
    lat_min: float,
    lon_max: float,
    lat_max: float,
) -> tuple[float, float, float, float]:
    corners = _TRANSFORMER.forward_many(
        (
            (lon_min, lat_min),
            (lon_min, lat_max),
            (lon_max, lat_min),
            (lon_max, lat_max),
        )
    )
    xs = [point[0] for point in corners]
    ys = [point[1] for point in corners]
    return min(xs), min(ys), max(xs), max(ys)


def test_capacity_guard_runs_before_grid_allocation() -> None:
    tracks = _crossing_tracks()
    cfg = IntersectConfig(cross_track_max_grid_cells_per_segment=1)

    with patch(
        "track.v2.cross_track._build_grid",
        side_effect=AssertionError("grid must not be allocated"),
    ):
        with pytest.raises(CrossTrackCapacityError, match="overflow fallback"):
            extract_cross_track_candidates(
                tracks,
                _TRANSFORMER,
                cfg,
                max_overflow_pair_space=1,
            )


def test_db_workspace_prefers_capacity_guarded_global_index() -> None:
    cfg = IntersectConfig()
    tracks = _crossing_tracks()
    extent = _projected_bounds(115.99, 38.99, 116.01, 39.01)

    with patch(
        "track.v2.detect.extract_cross_track_candidates",
        return_value=([], 1, 1),
    ) as global_extract, patch(
        "track.v2.detect._extract_cross_track_memory_windows",
        side_effect=AssertionError("fallback must not run"),
    ):
        assert _extract_cross_track_db_workspace(
            tracks, cfg, extent, _TRANSFORMER,
        ) == []

    call = global_extract.call_args
    assert call.args[1] is _TRANSFORMER
    global_cfg = call.args[2]
    assert global_cfg.cross_track_max_grid_cells_per_segment > (
        cfg.cross_track_max_grid_cells_per_segment
    )
    assert call.kwargs["max_grid_memberships"] > 0
    assert call.kwargs["max_overflow_pair_space"] > 0


def test_db_workspace_capacity_failure_uses_memory_not_sql() -> None:
    cfg = IntersectConfig()
    tracks = _crossing_tracks()
    extent = _projected_bounds(115.99, 38.99, 116.01, 39.01)
    sentinel = [object()]

    with patch(
        "track.v2.detect.extract_cross_track_candidates",
        side_effect=CrossTrackCapacityError("too dense"),
    ), patch(
        "track.v2.detect._extract_cross_track_memory_windows",
        return_value=sentinel,
    ) as fallback:
        assert _extract_cross_track_db_workspace(
            tracks, cfg, extent, _TRANSFORMER,
        ) is sentinel

    fallback.assert_called_once_with(
        tracks,
        cfg,
        extent,
        _TRANSFORMER,
        live_status=None,
        perf=None,
    )


def test_db_workspace_respects_disabled_cross_track_method() -> None:
    cfg = IntersectConfig(use_cross_track_candidates=False)

    with patch(
        "track.v2.detect.extract_cross_track_candidates",
        side_effect=AssertionError("Method 3 must stay disabled"),
    ):
        assert _extract_cross_track_db_workspace(
            _crossing_tracks(),
            cfg,
            _projected_bounds(115.99, 38.99, 116.01, 39.01),
            _TRANSFORMER,
        ) == []


def test_memory_windows_match_legacy_window_queries() -> None:
    origin_lon, origin_lat = 116.0, 39.0
    tracks = _crossing_tracks(origin_lon, origin_lat)
    all_points = [point for track in tracks.values() for point in track]
    all_points.sort(key=lambda point: (point.vehicle_id, point.timestamp))
    extent = _projected_bounds(115.98, 38.98, 116.02, 39.02)
    cfg = IntersectConfig(
        spatial_window_size=(1_000.0, 1_000.0),
        spatial_window_overlap=0.15,
    )

    def query(_conn: object, window: object, **_kwargs: object) -> list[TrajectoryPoint]:
        return [
            point
            for point in all_points
            if point.x is not None
            and point.y is not None
            and window.x_min <= point.x <= window.x_max
            and window.y_min <= point.y <= window.y_max
        ]

    with patch("track.v2.detect._query_active_points_in_window", side_effect=query):
        legacy = _extract_cross_track_windows(
            object(),
            cfg,
            extent,
            _SPATIAL_CONTEXT,
            _TRANSFORMER,
            live_status=_QuietStatus(),
        )
    memory = _extract_cross_track_memory_windows(
        tracks,
        cfg,
        extent,
        _TRANSFORMER,
        live_status=_QuietStatus(),
    )

    assert [_cross_track_candidate_key(item) for item in memory] == [
        _cross_track_candidate_key(item) for item in legacy
    ]


def test_window_shape_exact_multiple_has_no_phantom_column() -> None:
    step = 1_000.0
    windows = SpatialWindowIterator(
        (0.0, 0.0, 7.0 * step, 3.0 * step),
        size=(1_000.0, 1_000.0),
        overlap=0.15,
    )

    assert windows.window_shape() == (7, 3)
    assert windows.count_windows() == 21
    assert len(list(windows.iter_windows())) == 21


def test_window_iterator_count_always_matches_materialized_windows() -> None:
    step = 2_000.0
    for ulps, expected_lon_count in ((-1, 7), (0, 7), (1, 8)):
        edge = 7.0 * step
        if ulps < 0:
            edge = math.nextafter(edge, -math.inf)
        elif ulps > 0:
            edge = math.nextafter(edge, math.inf)
        windows = SpatialWindowIterator(
            (0.0, 0.0, edge, 2.3 * step),
            size=(2_000.0, 2_000.0),
            overlap=0.0,
        )
        materialized = list(windows.iter_windows())
        assert windows.window_shape()[0] == expected_lon_count
        assert windows.count_windows() == len(list(windows.iter_windows()))
        assert materialized[-1].x_max == edge


def test_tiny_positive_extent_still_has_one_window_per_axis() -> None:
    tiny = math.nextafter(0.0, math.inf)
    windows = SpatialWindowIterator((0.0, 0.0, tiny, tiny))

    assert windows.window_shape() == (1, 1)
    assert windows.count_windows() == 1
