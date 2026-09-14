from dataclasses import dataclass, replace

from track.v2.models import PointRef, TrajectoryPoint
from track.v2.refine import _projected_point


def test_projection_copy_matches_replace_without_rebuilding_identity():
    point = TrajectoryPoint(PointRef("source", "12", True), "v", 1, 116, 39, 90, 10, {"a": 1})
    result = _projected_point(point, 3, 4, "v_seg01")
    assert result == replace(point, x=3, y=4, vehicle_id="v_seg01")
    assert result.point_ref is point.point_ref
    assert result.extra is point.extra
    assert point.x is None and point.vehicle_id == "v"


def test_projection_copy_preserves_subclass_fields():
    @dataclass(frozen=True)
    class TaggedPoint(TrajectoryPoint):
        tag: str = "custom"

    point = TaggedPoint(PointRef("source", "12", True), "v", 1, 116, 39, 90, 10)
    assert _projected_point(point, 3, 4, "v_seg01") == replace(point, x=3, y=4, vehicle_id="v_seg01")
