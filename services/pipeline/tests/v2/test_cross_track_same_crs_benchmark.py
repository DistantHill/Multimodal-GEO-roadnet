from __future__ import annotations

from .config_factory import intersect_run_config

import pytest
from unittest.mock import patch

from track.v2.cross_track import (
    extract_cross_track_candidates_reference,
)
from track.v2.detect_candidates import (
    extract_cross_track_candidates,
)
from track.v2.db.queries import SpatialWindow
from track.v2.detect import IntersectConfig
from track.v2.manual_thin_benchmark.manual_bench_cross_track_inner import (
    _compare_records,
    _run_stage,
)
from track.v2.models import RefinedPointRef, PointRef, SpatialContext, TrajectoryPoint
from track.v2.projection import CoordinateTransformer


def test_same_crs_comparison_accepts_tiny_absolute_roundoff_and_reports_it() -> None:
    left = [{"id": "a|b", "members": ["a", "b"], "x": 500000.0, "lon": 116.0}]
    right = [{"id": "a|b", "members": ["a", "b"], "x": 500000.0 + 5e-7, "lon": 116.0 + 5e-10}]

    differences = _compare_records(left, right)

    assert differences["x"] == pytest.approx(5e-7, rel=0, abs=1e-10)
    assert differences["lon"] == pytest.approx(5e-10, rel=0, abs=1e-13)


@pytest.mark.parametrize(
    "right,match",
    [
        ([{"id": "a|b", "members": ["b", "a"], "x": 1.0}], "identity/member/order"),
        ([{"id": "a|b", "members": ["a", "b"], "x": float("nan")}], "non-finite"),
        ([{"id": "a|b", "members": ["a", "b"], "x": 1.00001}], "tolerance"),
    ],
)
def test_same_crs_comparison_fails_closed(right: object, match: str) -> None:
    left = [{"id": "a|b", "members": ["a", "b"], "x": 1.0}]
    with pytest.raises(ValueError, match=match):
        _compare_records(left, right)


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
    with patch("track.v2.detect._query_active_points_in_window", return_value=points):
        for extractor in (extract_cross_track_candidates_reference, extract_cross_track_candidates):
            candidates, _wall, _telemetry = _run_stage(
                object(), cfg,
                (window.x_min, window.y_min, window.x_max, window.y_max),
                context, transformer, extractor, windows_override=[window],
            )
            results.append([candidate.as_dict() for candidate in candidates])

    _compare_records(results[0], results[1])
    assert [candidate["vehicle_id"] for candidate in results[1]] == ["A|B", "A|B"]
