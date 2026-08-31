from __future__ import annotations

import math
from typing import Any
from uuid import UUID

import pytest

from backend.map_api import (
    MapNotFoundError,
    PostgresMapStore,
    _Intersection,
    _TrackPoint,
    _clip_track,
    _complete_tracks,
    _crossings,
)
from backend.map_schemas import ClipRequest, ReplayRequest


class ScriptedCursor:
    def __init__(self, connection: "ScriptedConnection", rows: list[Any]) -> None:
        self.connection = connection
        self.rows = rows

    def __enter__(self) -> "ScriptedCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.connection.executions.append((sql, params))

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[Any]:
        return list(self.rows)


class ScriptedConnection:
    def __init__(self, *responses: list[Any]) -> None:
        self.responses = list(responses)
        self.executions: list[tuple[str, tuple[Any, ...]]] = []

    def cursor(self) -> ScriptedCursor:
        if not self.responses:
            raise AssertionError("unexpected database cursor")
        return ScriptedCursor(self, self.responses.pop(0))


def point_row(
    vehicle_id: str,
    subtrack_index: int,
    point_id: int,
    timestamp: float,
    lon: float,
    lat: float = 0.0,
) -> tuple[Any, ...]:
    return (vehicle_id, subtrack_index, point_id, timestamp, lon, lat, 0.0, 10.0, str(point_id), False)


def track_point(lon: float, timestamp: float) -> _TrackPoint:
    return _TrackPoint(None, timestamp, lon, 0.0, 0.0, 10.0, None, False)


def intersections() -> list[_Intersection]:
    return [
        _Intersection(101, 10, 0.0, 0.0, 100.0),
        _Intersection(202, 20, 0.01, 0.0, 100.0),
    ]


def test_intersection_layer_resolves_latest_completed_detect_and_parameterizes_bbox() -> None:
    polygon = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 0]]]}
    connection = ScriptedConnection(
        [["detect-latest", "clean-latest", "trajectory_points"]],
        [[10, 101, 120.2, 30.3, 25.0, [0, 90], {"0->90": 4}, 4, "ok", polygon]],
    )
    store = PostgresMapStore(connection)

    result = store.read_layer(
        "intersections",
        bbox=(120.0, 30.0, 121.0, 31.0),
        detect_id=None,
        fit_id=None,
        limit=100,
    )

    assert result["metadata"]["detect_id"] == "detect-latest"
    assert result["metadata"]["clean_id"] == "clean-latest"
    assert result["features"][0]["id"] == 10
    assert result["features"][0]["properties"]["detect_id"] == "detect-latest"
    assert "d.status = 'completed'" in connection.executions[0][0]
    assert connection.executions[0][1] == ()
    layer_sql, params = connection.executions[1]
    assert "ST_MakeEnvelope(%s, %s, %s, %s, 4326)" in layer_sql
    assert "120.0" not in layer_sql
    assert params == ("detect-latest", 120.0, 30.0, 121.0, 31.0, 101)


def test_centerline_layer_resolves_latest_completed_fit() -> None:
    geometry = {"type": "LineString", "coordinates": [[120.0, 30.0], [120.1, 30.1]]}
    connection = ScriptedConnection(
        [["fit-latest", "detect-owner"]],
        [[9, "detect-owner", "combined", 1, 2, 120.0, 3, 4, geometry]],
    )

    result = PostgresMapStore(connection).read_layer(
        "centerlines", bbox=None, detect_id=None, fit_id=None, limit=5
    )

    assert result["metadata"]["fit_id"] == "fit-latest"
    assert result["metadata"]["detect_id"] == "detect-owner"
    assert connection.executions[1][1] == ("fit-latest", 6)


def test_entry_exit_feature_carries_detect_context() -> None:
    geometry = {"type": "Point", "coordinates": [120.0, 30.0]}
    connection = ScriptedConnection(
        [["detect-1", "clean-1", "trajectory_points"]],
        [["8:entry", 8, 10, "vehicle-1", "entry", 45.0, geometry]],
    )

    result = PostgresMapStore(connection).read_layer(
        "entry_exit_points", bbox=None, detect_id="detect-1", fit_id=None, limit=5
    )

    properties = result["features"][0]["properties"]
    assert properties["detect_id"] == "detect-1"
    assert properties["cluster_id"] == 10


def test_replay_point_ids_reports_partial_resolution_and_returns_ordered_track() -> None:
    connection = ScriptedConnection(
        [[1]],
        [["vehicle-1", 0]],
        [
            point_row("vehicle-1", 0, 1, 10.0, 120.0),
            point_row("vehicle-1", 0, 2, 11.0, 120.001),
        ],
    )
    request = ReplayRequest(point_ids=[1, 99], max_vehicles=5, max_points=10)

    result = PostgresMapStore(connection).replay(request)

    assert result["metadata"]["requested_point_count"] == 2
    assert result["metadata"]["resolved_point_count"] == 1
    assert result["metadata"]["missing_point_ids"] == [99]
    assert result["features"][0]["geometry"]["type"] == "LineString"
    assert result["features"][0]["properties"]["timestamps"] == [10.0, 11.0]
    for sql, _ in connection.executions:
        assert "99" not in sql
        assert "INSERT" not in sql.upper()
        assert "UPDATE" not in sql.upper()
        assert "DELETE" not in sql.upper()


def test_replay_unknown_point_ids_is_not_found() -> None:
    store = PostgresMapStore(ScriptedConnection([]))

    with pytest.raises(MapNotFoundError, match="none of the requested point_ids"):
        store.replay(ReplayRequest(point_ids=[999]))


def test_clip_reads_bounded_clean_tracks_and_returns_geojson() -> None:
    intersection_rows = [
        [101, 10, 0.0, 0.0, 100.0],
        [202, 20, 0.01, 0.0, 100.0],
    ]
    track_rows = [
        point_row("vehicle-1", 1, 1, 0.0, -0.002),
        point_row("vehicle-1", 1, 2, 1.0, 0.005),
        point_row("vehicle-1", 1, 3, 2.0, 0.012),
    ]
    connection = ScriptedConnection(
        [[
            "12345678-1234-5678-1234-567812345678",
            "clean-1",
            "trajectory_points",
        ]],
        intersection_rows,
        [["vehicle-1", 1]],
        track_rows,
    )
    request = ClipRequest(
        detect_id=UUID("12345678-1234-5678-1234-567812345678"),
        intersection_ids=[10, 20],
        max_vehicles=5,
        max_points=10,
    )

    result = PostgresMapStore(connection).clip(request)

    assert result["metadata"]["returned_clip_count"] == 1
    assert result["metadata"]["truncated"] is False
    feature = result["features"][0]
    assert feature["properties"]["from_intersection_id"] == 10
    assert feature["properties"]["to_intersection_id"] == 20
    assert feature["properties"]["distance_m"] > 0.0
    assert feature["geometry"]["type"] == "LineString"
    assert connection.executions[2][1][-1] == 6
    assert connection.executions[3][1][-1] == 11


def test_clip_geometry_supports_both_directions_and_one_long_segment() -> None:
    forward = [track_point(-0.002, 0.0), track_point(0.012, 1.0)]
    reverse = list(reversed(forward))

    forward_features = _clip_track(("v", 1), forward, intersections(), "detect")
    reverse_features = _clip_track(("v", 1), reverse, intersections(), "detect")

    assert len(forward_features) == len(reverse_features) == 1
    assert forward_features[0]["properties"]["from_intersection_id"] == 10
    assert forward_features[0]["properties"]["to_intersection_id"] == 20
    assert reverse_features[0]["properties"]["from_intersection_id"] == 20
    assert reverse_features[0]["properties"]["to_intersection_id"] == 10
    assert len(forward_features[0]["geometry"]["coordinates"]) == 2


def test_same_intersection_reentry_does_not_create_a_self_clip() -> None:
    points = [
        track_point(-0.002, 0.0),
        track_point(0.002, 1.0),
        track_point(-0.0005, 2.0),
        track_point(0.002, 3.0),
        track_point(0.012, 4.0),
    ]

    features = _clip_track(("v", 1), points, intersections(), "detect")

    assert features
    assert all(
        feature["properties"]["from_intersection_id"]
        != feature["properties"]["to_intersection_id"]
        for feature in features
    )
    assert all(
        {
            feature["properties"]["from_intersection_id"],
            feature["properties"]["to_intersection_id"],
        }
        == {10, 20}
        for feature in features
    )


def test_tangent_segment_does_not_create_crossing_events() -> None:
    radius_degrees = 100.0 / 111_195.0
    points = [
        _TrackPoint(None, 0.0, -0.002, radius_degrees, 0.0, 0.0, None, False),
        _TrackPoint(None, 1.0, 0.002, radius_degrees, 0.0, 0.0, None, False),
    ]

    events = _crossings(points, [intersections()[0]])

    assert events == []


def test_point_limit_drops_a_partial_final_track_and_marks_truncation() -> None:
    rows = [
        point_row("a", 1, 1, 0.0, 0.0),
        point_row("a", 1, 2, 1.0, 0.001),
        point_row("b", 1, 3, 0.0, 0.01),
        point_row("b", 1, 4, 1.0, 0.011),
    ]

    grouped, truncated = _complete_tracks(rows, 3)

    assert truncated is True
    assert list(grouped) == [("a", 1)]
    assert len(grouped[("a", 1)]) == 2


def test_bbox_and_any_values_are_always_bound_parameters() -> None:
    connection = ScriptedConnection([["v", 0]])
    store = PostgresMapStore(connection)

    selected, _ = store._select_raw_tracks(
        point_ids=None,
        bbox=(120.1234567, 30.0, 121.0, 31.0),
        limit=7,
    )

    assert selected == [("v", 0)]
    sql, params = connection.executions[0]
    assert "120.1234567" not in sql
    assert sql.count("%s") == 5
    assert params == (120.1234567, 30.0, 121.0, 31.0, 8)
