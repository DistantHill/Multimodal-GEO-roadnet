"""Public field regression gates before internal patch persistence changes."""

from __future__ import annotations

import json

import pytest

from backend.api.map_routes import run_catalog
from backend.stores.map_layers import PostgresMapStore


REFINE_ID = "12345678-1234-5678-1234-567812345670"
DETECT_ID = "12345678-1234-5678-1234-567812345671"
FIT_ID = "12345678-1234-5678-1234-567812345672"
POINT = {"type": "Point", "coordinates": [120.1, 30.2]}


class RunCatalogStore(PostgresMapStore):
    def __init__(self, *, empty: bool = False) -> None:
        super().__init__(None)
        self.empty = empty
        self.queries: list[str] = []

    def _fetchall(self, sql: str, params: tuple) -> list[tuple]:
        self.queries.append(sql)
        assert params == ()
        # Internal working patches must not populate the public Run selectors.
        assert "WHERE status = 'completed'" in sql
        if self.empty:
            return []
        if "FROM refine_runs" in sql:
            return [(REFINE_ID, "2026-09-13T00:00:00+00:00")]
        if "FROM detect_runs" in sql:
            return [(DETECT_ID, REFINE_ID, "2026-09-13T00:01:00+00:00")]
        if "FROM fit_runs" in sql:
            return [(FIT_ID, DETECT_ID, "2026-09-13T00:02:00+00:00")]
        raise AssertionError(sql)


def test_run_catalog_keeps_frontend_linked_uuid_fields() -> None:
    result = run_catalog(store=RunCatalogStore())
    assert set(result) == {"refine", "detect", "fit"}
    assert result["refine"][0]["id"] == REFINE_ID
    assert result["detect"][0]["refine_id"] == REFINE_ID
    assert result["detect"][0]["id"] == DETECT_ID
    assert result["fit"][0]["detect_id"] == DETECT_ID
    assert result["fit"][0]["id"] == FIT_ID
    assert all(isinstance(rows[0]["started_at"], str) for rows in result.values())


def test_rebuilt_empty_database_has_all_run_selector_arrays() -> None:
    assert run_catalog(store=RunCatalogStore(empty=True)) == {
        "refine": [], "detect": [], "fit": [],
    }


@pytest.mark.parametrize("source_key", [None, "00042", "external-A"])
def test_resolved_point_identity_and_nullable_crossings(source_key: str | None) -> None:
    row = (2, "vehicle-1", 41, source_key, "p", None, None, None, json.dumps(POINT))
    result = PostgresMapStore._resolved_point_feature(row, DETECT_ID)
    assert result == {
        "type": "Feature",
        "id": 41,
        "geometry": POINT,
        "properties": {
            "detect_id": DETECT_ID,
            "vehicle_id": "vehicle-1",
            "subtrack_index": 2,
            "resolved_point_id": 41,
            "source_row_key": source_key,
            "point_kind": "p",
            "crossing_id": None,
            "intersection_id": None,
            "crossing_type": None,
        },
    }


def test_intersection_feature_contract_is_exact() -> None:
    polygon = {
        "type": "Polygon",
        "coordinates": [[[120.0, 30.0], [120.2, 30.0], [120.0, 30.0]]],
    }
    result = PostgresMapStore._intersection_feature(
        (17, 120.1, 30.2, 35.5, "[10.0, 95.0]", '{"10->95": 4}', 6,
         '{"vehicle-1": 3}', '["vehicle-1"]', polygon),
        DETECT_ID,
    )
    assert result == {
        "type": "Feature",
        "id": 17,
        "geometry": polygon,
        "properties": {
            "detect_id": DETECT_ID,
            "intersection_id": 17,
            "centroid": [120.1, 30.2],
            "clip_radius_m": 35.5,
            "branch_centers_deg": [10.0, 95.0],
            "transition_counts": {"10->95": 4},
            "observation_count": 6,
            "contributors": {"vehicle-1": 3},
            "critical_contributors": ["vehicle-1"],
        },
    }


@pytest.mark.parametrize("kind, label", [(1, "entry"), (2, "exit")])
def test_crossing_fields_keep_resolved_identity(kind: int, label: str) -> None:
    result = PostgresMapStore._entry_exit_feature((7, 8, 9, 10, kind, POINT), DETECT_ID)
    assert result["properties"] == {
        "detect_id": DETECT_ID,
        "crossing_id": 7,
        "intersection_id": 8,
        "resolved_point_id": 9,
        "source_row_key": None,
        "subtrack_index": 10,
        "crossing_type": kind,
        "crossing_kind": label,
    }


def test_centerline_fields_keep_pair_direction_and_metre_units() -> None:
    geometry = {"type": "LineString", "coordinates": [[120.1, 30.2], [120.2, 30.3]]}
    result = PostgresMapStore._centerline_feature(
        (81, DETECT_ID, "directed", 11, 12, 123.5, 8, 3, geometry),
    )
    assert result["id"] == 81
    assert result["geometry"] == geometry
    assert result["properties"] == {
        "detect_id": DETECT_ID,
        "variant": "directed",
        "from_intersection_id": 11,
        "to_intersection_id": 12,
        "length_m": 123.5,
        "forward_clip_count": 8,
        "reverse_clip_count": 3,
    }
