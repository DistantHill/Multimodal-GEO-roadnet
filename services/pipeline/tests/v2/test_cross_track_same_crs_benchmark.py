from __future__ import annotations

from .config_factory import intersect_run_config

import pytest
from unittest.mock import patch

from track.v2.candidates.detect_cross_track_pairs import (
    extract_cross_track_candidates_reference,
)
from track.v2.candidates.collect_candidates import (
    extract_cross_track_candidates,
)
from track.v2.projection import SpatialWindow
from track.v2.intersections.detect_intersections import IntersectConfig, _extract_cross_track_windows
from track.v2.models import RefinedPointRef, PointRef, SpatialContext, TrajectoryPoint
from track.v2.projection import CoordinateTransformer


def test_complete_stage_reference_matches_db_subtrack_keys_and_vehicle_identity() -> None:
    context = SpatialContext(
        4326, 32650,
        {"coordinate_semantics_version": 2, "enforce_area_of_use": True, "max_scale_error": .01},
    )
    transformer = CoordinateTransformer(context)
    next_id = 1

    def point(vehicle: str, subtrack: int, x: float, y: float, heading: float) -> TrajectoryPoint:
        nonlocal next_id
        identity = next_id
        next_id += 1
        lon, lat = transformer.inverse(x, y)
        return TrajectoryPoint(
            PointRef("fixture", str(identity), True), vehicle, float(identity), lon, lat,
            heading, 8.0, x=x, y=y,
            refined_point_ref=RefinedPointRef("kept", identity, subtrack, identity),
        )

    points = [
        point("A", 1, 499900.0, 4319900.0, 90.0),
        point("A", 1, 500100.0, 4319900.0, 90.0),
        point("A", 2, 499900.0, 4320100.0, 90.0),
        point("A", 2, 500100.0, 4320100.0, 90.0),
        point("B", 3, 500000.0, 4319800.0, 0.0),
        point("B", 3, 500000.0, 4320200.0, 0.0),
    ]
    cfg = intersect_run_config(use_turn_events=False, cross_track_grid_cell_m=100.0)
    window = SpatialWindow(499000.0, 4319000.0, 501000.0, 4321000.0)
    results = []
    with patch("track.v2.intersections.detect_intersections._query_active_points_in_window", return_value=points):
        for extractor in (extract_cross_track_candidates_reference, extract_cross_track_candidates):
            candidates = _extract_cross_track_windows(
                object(), cfg,
                (window.x_min, window.y_min, window.x_max, window.y_max),
                context, transformer, candidate_extractor=extractor,
                windows_override=[window],
            )
            results.append(candidates)

    reference, optimized = results
    assert [candidate.vehicle_id for candidate in reference] == ["A|B", "A|B"]
    assert [candidate.vehicle_id for candidate in optimized] == ["A|B", "A|B"]
    for expected, actual in zip(reference, optimized, strict=True):
        assert actual.from_segment_start == expected.from_segment_start
        assert actual.to_segment_start == expected.to_segment_start
        assert actual.timestamp == expected.timestamp
        assert actual.heading == expected.heading
        assert actual.speed == expected.speed
        assert actual.x == pytest.approx(expected.x, rel=0, abs=1e-6)
        assert actual.y == pytest.approx(expected.y, rel=0, abs=1e-6)
        assert actual.lon == pytest.approx(expected.lon, rel=0, abs=1e-9)
        assert actual.lat == pytest.approx(expected.lat, rel=0, abs=1e-9)
