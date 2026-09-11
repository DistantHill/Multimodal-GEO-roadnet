from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

import pytest

from backend.map_api import (
    MapDataError,
    MapNotFoundError,
    PostgresMapStore,
    _CleanContext,
    _CoordinateTransformer,
    _DetectContext,
    _Intersection,
    _TrackPoint,
    _clip_track,
    _complete_tracks,
    _limit_selected_tracks,
)
from backend.map_schemas import ClipRequest, ReplayRequest


_EMPTY_FINGERPRINT = {
    "version": 1,
    "row_count": 0,
    "sha256": hashlib.sha256().hexdigest(),
}
_PROJECTION_CONFIG = {
    "coordinate_semantics_version": 2,
    "enforce_area_of_use": False,
    "max_scale_error": 1.0,
}
_CLEAN = _CleanContext(
    clean_id="clean-1",
    source_table="public.source_points",
    quoted_source_table='"public"."source_points"',
    source_key_sql_type="bigint",
    source_fingerprint=_EMPTY_FINGERPRINT,
)
_DETECT = _DetectContext(
    detect_id="detect-1",
    clean=_CLEAN,
    source_srid=4326,
    processing_srid=3857,
    projection_config=_PROJECTION_CONFIG,
)
_TRANSFORMER = _CoordinateTransformer(_DETECT)
_METERS_PER_DEGREE = 111_319.49079327358


def _lon(x_m: float) -> float:
    return x_m / _METERS_PER_DEGREE


def detect_row(
    detect_id: str,
    clean_id: str,
    *,
    processing_srid: int | None = 3857,
    projection_config: dict[str, Any] | None = None,
) -> list[Any]:
    return [
        detect_id,
        clean_id,
        "public.source_points",
        _EMPTY_FINGERPRINT,
        4326,
        processing_srid,
        _PROJECTION_CONFIG if projection_config is None else projection_config,
    ]


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


def resolved_row(
    subtrack_index: int,
    vehicle_id: str,
    resolved_point_id: int,
    lon: float,
    *,
    source_row_key: str | None = None,
    point_kind: str = "kept",
    crossing_id: int | None = None,
    intersection_id: int | None = None,
    crossing_type: int | None = None,
) -> tuple[Any, ...]:
    return (
        subtrack_index,
        vehicle_id,
        resolved_point_id,
        source_row_key,
        lon,
        0.0,
        point_kind,
        crossing_id,
        intersection_id,
        crossing_type,
    )


def track_point(
    resolved_point_id: int,
    x_m: float,
    *,
    source_row_key: str | None = None,
    point_kind: str = "kept",
    crossing_id: int | None = None,
    intersection_id: int | None = None,
    crossing_type: int | None = None,
) -> _TrackPoint:
    return _TrackPoint(
        resolved_point_id=resolved_point_id,
        source_row_key=source_row_key,
        lon=_lon(x_m),
        lat=0.0,
        point_kind=point_kind,
        crossing_id=crossing_id,
        intersection_id=intersection_id,
        crossing_type=crossing_type,
    )


def intersections() -> list[_Intersection]:
    return [
        _Intersection(101, _lon(-50.0), 0.0, 20.0),
        _Intersection(202, _lon(50.0), 0.0, 20.0),
    ]


def clip_points() -> list[_TrackPoint]:
    return [
        track_point(1, -100.0, source_row_key="source-a"),
        track_point(
            2,
            -70.0,
            point_kind="crossing",
            crossing_id=11,
            intersection_id=101,
            crossing_type=1,
        ),
        track_point(
            3,
            -30.0,
            point_kind="crossing",
            crossing_id=12,
            intersection_id=101,
            crossing_type=0,
        ),
        track_point(4, 0.0, source_row_key="source-b"),
        track_point(
            5,
            30.0,
            point_kind="crossing",
            crossing_id=13,
            intersection_id=202,
            crossing_type=1,
        ),
        track_point(
            6,
            70.0,
            point_kind="crossing",
            crossing_id=14,
            intersection_id=202,
            crossing_type=0,
        ),
        track_point(7, 100.0, source_row_key="source-c"),
    ]


def test_intersection_layer_uses_complete_detect_and_contributor_fields() -> None:
    polygon = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 0]]]}
    connection = ScriptedConnection(
        [detect_row("detect-latest", "clean-latest")],
        [["int8"]],
        [[101, 120.2, 30.3, 25.0, [0, 90], {"0->90": 4}, 4,
          {"turn": {"count": 4, "ratio": 1.0}}, ["turn"], polygon]],
    )

    result = PostgresMapStore(connection).read_layer(
        "intersections",
        bbox=(120.0, 30.0, 121.0, 31.0),
        detect_id=None,
        fit_id=None,
        limit=100,
    )

    feature = result["features"][0]
    assert result["metadata"]["detect_id"] == "detect-latest"
    assert feature["properties"]["contributors"]["turn"]["count"] == 4
    assert feature["properties"]["critical_contributors"] == ["turn"]
    assert "cluster_id" not in feature["properties"]
    layer_sql, params = connection.executions[2]
    assert "candidate.contributors" in layer_sql
    assert "ST_MakeEnvelope(%s, %s, %s, %s, 4326)" in layer_sql
    assert "120.0" not in layer_sql
    assert params == ("detect-latest", 120.0, 30.0, 121.0, 31.0, 101)


def test_resolved_layer_exposes_both_identity_names_without_vehicle_storage() -> None:
    geometry = {"type": "Point", "coordinates": [120.0, 30.0]}
    connection = ScriptedConnection(
        [detect_row("detect-1", "clean-1")],
        [["int8"]],
        [[7, "vehicle-1", 91, "external-key", "kept", None, None, None, geometry]],
    )

    result = PostgresMapStore(connection).read_layer(
        "resolved_subtrack_points",
        bbox=None,
        detect_id="detect-1",
        fit_id=None,
        limit=5,
    )

    properties = result["features"][0]["properties"]
    assert properties["resolved_point_id"] == 91
    assert properties["source_row_key"] == "external-key"
    sql = connection.executions[2][0]
    assert "resolved_subtrack_points" in sql
    assert "point.vehicle_id" not in sql
    assert "COALESCE(kept.vehicle_id, injected.vehicle_id)" in sql


def test_entry_exit_layer_maps_persisted_crossing_identity() -> None:
    geometry = {"type": "Point", "coordinates": [120.0, 30.0]}
    connection = ScriptedConnection(
        [detect_row("detect-1", "clean-1")],
        [["int8"]],
        [[8, 101, 91, 7, 1, geometry]],
    )

    result = PostgresMapStore(connection).read_layer(
        "entry_exit_points", bbox=None, detect_id="detect-1", fit_id=None, limit=5
    )

    properties = result["features"][0]["properties"]
    assert properties["crossing_id"] == 8
    assert properties["resolved_point_id"] == 91
    assert properties["crossing_kind"] == "entry"


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


def test_resolved_replay_reports_missing_ids_and_returns_full_track() -> None:
    detect_id = "12345678-1234-5678-1234-567812345678"
    connection = ScriptedConnection(
        [detect_row(detect_id, "clean-1")],
        [["int8"]],
        [["vehicle-1", 7, 1]],
        [
            resolved_row(7, "vehicle-1", 1, 120.0, source_row_key="external-1"),
            resolved_row(7, "vehicle-1", 2, 120.001, source_row_key="external-2"),
        ],
    )
    request = ReplayRequest(
        detect_id=UUID(detect_id),
        resolved_point_ids=[1, 99],
        max_vehicles=5,
        max_points=10,
    )

    result = PostgresMapStore(connection).replay(request)

    assert result["metadata"]["selection"] == "resolved_point_ids"
    assert result["metadata"]["missing_resolved_point_ids"] == [99]
    feature = result["features"][0]
    assert feature["properties"]["resolved_point_ids"] == [1, 2]
    assert feature["properties"]["source_row_keys"] == ["external-1", "external-2"]
    for sql, _ in connection.executions:
        assert "WITH referenced" not in sql
        assert "subtrack_vehicle" not in sql
        assert "99" not in sql
        assert not any(word in sql.upper() for word in ("INSERT", "UPDATE", "DELETE"))
    fetch_sql, fetch_params = connection.executions[-1]
    assert "WITH selected AS MATERIALIZED" in fetch_sql
    assert "subtrack_index = ANY(%s::integer[])" in fetch_sql
    assert fetch_params[:3] == (detect_id, [7], 11)


def test_source_key_replay_works_without_detect_context() -> None:
    connection = ScriptedConnection(
        [["clean-1", "public.source_points", _EMPTY_FINGERPRINT]],
        [["int8"]],
        [["vehicle-1", 7, "external-1"]],
        [
            resolved_row(7, "vehicle-1", 0, 120.0, source_row_key="external-1"),
            resolved_row(7, "vehicle-1", 0, 120.001, source_row_key=None,
                         point_kind="injected"),
        ],
    )

    result = PostgresMapStore(connection).replay(
        ReplayRequest(source_row_keys=["external-1"], max_points=10)
    )

    assert result["metadata"]["detect_id"] is None
    assert result["metadata"]["missing_source_row_keys"] == []
    assert result["features"][0]["properties"]["source_row_keys"] == [
        "external-1",
        None,
    ]


def test_unknown_resolved_ids_are_not_found_after_context_validation() -> None:
    detect_id = "12345678-1234-5678-1234-567812345678"
    connection = ScriptedConnection(
        [detect_row(detect_id, "clean-1")],
        [["int8"]],
        [],
    )
    with pytest.raises(MapNotFoundError, match="no replay points match resolved_point_ids"):
        PostgresMapStore(connection).replay(
            ReplayRequest(detect_id=UUID(detect_id), resolved_point_ids=[999])
        )


def test_store_clip_reads_resolved_crossings_and_returns_between_clip() -> None:
    detect_id = "12345678-1234-5678-1234-567812345678"
    intersection_rows = [
        [item.intersection_id, item.lon, item.lat, item.radius_m]
        for item in intersections()
    ]
    rows = [
        resolved_row(
            7,
            "vehicle-1",
            point.resolved_point_id or 0,
            point.lon,
            source_row_key=point.source_row_key,
            point_kind=point.point_kind,
            crossing_id=point.crossing_id,
            intersection_id=point.intersection_id,
            crossing_type=point.crossing_type,
        )
        for point in clip_points()
    ]
    connection = ScriptedConnection(
        [detect_row(detect_id, "clean-1")],
        [["int8"]],
        [[False]],
        [],
        intersection_rows,
        [["vehicle-1", 7, None]],
        rows,
    )

    result = PostgresMapStore(connection).clip(
        ClipRequest(
            detect_id=UUID(detect_id),
            intersection_ids=[101, 202],
            max_vehicles=5,
            max_points=20,
        )
    )

    assert result["metadata"]["returned_clip_count"] == 1
    feature = result["features"][0]
    assert feature["properties"]["clip_type"] == "between_intersection"
    assert feature["properties"]["start_crossing_id"] == 12
    assert feature["properties"]["end_crossing_id"] == 13
    assert feature["properties"]["resolved_point_ids"] == [4]
    assert feature["properties"]["source_row_keys"] == ["source-b"]


def test_clip_track_batches_same_position_exit_then_entry_without_false_road() -> None:
    points = [
        track_point(1, -50.0),
        track_point(
            2, -30.0, point_kind="crossing", crossing_id=11,
            intersection_id=101, crossing_type=0,
        ),
        track_point(
            3, -30.0, point_kind="crossing", crossing_id=12,
            intersection_id=202, crossing_type=1,
        ),
        track_point(
            4, 70.0, point_kind="crossing", crossing_id=13,
            intersection_id=202, crossing_type=0,
        ),
        track_point(5, 100.0),
    ]
    custom_intersections = [
        _Intersection(101, _lon(-50.0), 0.0, 20.0),
        _Intersection(202, _lon(-10.0), 0.0, 20.0),
    ]

    features = _clip_track(
        ("vehicle-1", 7),
        points,
        custom_intersections,
        {101, 202},
        "detect-1",
        _TRANSFORMER,
    )

    assert features == []


def test_clip_rejects_detect_without_fixed_processing_crs() -> None:
    detect_id = "12345678-1234-5678-1234-567812345678"
    connection = ScriptedConnection(
        [detect_row(detect_id, "clean-1", processing_srid=None)],
        [["int8"]],
    )
    with pytest.raises(MapDataError, match="has no processing CRS"):
        PostgresMapStore(connection).clip(
            ClipRequest(detect_id=UUID(detect_id), intersection_ids=[101, 202])
        )


def test_source_fingerprint_rejects_missing_referenced_row() -> None:
    connection = ScriptedConnection([[False]], [["missing", False, None, None, None,
                                                  None, None, None, {}]])
    store = PostgresMapStore(connection)
    with pytest.raises(MapDataError, match="missing source row"):
        store._verify_source_fingerprint(_CLEAN)


def test_coordinate_transformer_rejects_invalid_context_and_distortion() -> None:
    with pytest.raises(MapDataError, match="area of use"):
        _CoordinateTransformer(
            _DetectContext(
                "detect",
                _CLEAN,
                4326,
                32650,
                {
                    "coordinate_semantics_version": 2,
                    "enforce_area_of_use": True,
                    "max_scale_error": 1.0,
                },
            )
        ).forward_many([(110.0, 36.0), (116.0, 39.0)])

    with pytest.raises(MapDataError, match="scale distortion"):
        _CoordinateTransformer(
            _DetectContext(
                "detect",
                _CLEAN,
                4326,
                3857,
                {
                    "coordinate_semantics_version": 2,
                    "enforce_area_of_use": False,
                    "max_scale_error": 0.01,
                },
            )
        ).forward_many([(116.0, 39.0), (116.1, 39.1)])


def test_point_limit_drops_partial_final_track() -> None:
    rows = [
        resolved_row(1, "a", 1, 0.0),
        resolved_row(1, "a", 2, 0.001),
        resolved_row(2, "b", 3, 0.01),
        resolved_row(2, "b", 4, 0.011),
    ]
    grouped, truncated = _complete_tracks(rows, 3)
    assert truncated is True
    assert list(grouped) == [("a", 1)]


def test_track_limit_collects_identity_before_vehicle_truncation() -> None:
    selected, found, truncated = _limit_selected_tracks(
        [["a", 1, 10], ["b", 2, 20]],
        max_vehicles=1,
    )
    assert selected == [1]
    assert found == {10, 20}
    assert truncated is True


def test_dynamic_values_are_bound_parameters() -> None:
    connection = ScriptedConnection([["vehicle-1", 7, None]])
    store = PostgresMapStore(connection)
    selected, _, _ = store._select_resolved_tracks(
        _DETECT,
        resolved_point_ids=[1201234567],
        source_row_keys=None,
        bbox=None,
        max_vehicles=7,
    )
    assert selected == [7]
    sql, params = connection.executions[0]
    assert "1201234567" not in sql
    assert params == ("detect-1", [1201234567])
