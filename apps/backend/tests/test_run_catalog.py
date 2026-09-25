from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from backend.api.map_routes import run_catalog, read_layer
from backend.config import MapConfig
from backend.map_errors import MapDataError


MAP_CONFIG = MapConfig(layer_limit_default=1_000, layer_limit_max=5_000,
                       catalog_exact_count_max_rows=50_000,
                       replay_max_vehicles_default=25, replay_max_vehicles_max=100,
                       replay_max_points_default=10_000, replay_max_points_max=50_000,
                       replay_selection_ids_max=1_000,
                       clip_max_vehicles_default=50, clip_max_vehicles_max=200,
                       clip_max_points_default=20_000, clip_max_points_max=100_000,
                       clip_feature_limit=1_000)


def test_run_catalog_preserves_parent_relationships():
    store = Mock()
    timestamp = "2026-09-11T12:00:00+00:00"
    store.run_catalog.return_value = {"refine": [{"id": "c1", "started_at": timestamp}], "detect": [{"id": "d1", "refine_id": "c1", "started_at": timestamp}], "fit": [{"id": "f1", "detect_id": "d1", "started_at": timestamp}]}
    result = run_catalog(store)
    assert result == {"refine": [{"id": "c1", "started_at": timestamp}],
                      "detect": [{"id": "d1", "refine_id": "c1", "started_at": timestamp}],
                      "fit": [{"id": "f1", "detect_id": "d1", "started_at": timestamp}]}
    store.run_catalog.assert_called_once_with()


def test_layer_rejects_mismatched_refine_before_loading():
    store = Mock()
    store.validate_layer_context.side_effect = MapDataError("mismatch")
    with pytest.raises(HTTPException) as error:
        read_layer("intersections", bbox=None, detect_id="d1", fit_id=None,
                   refine_id="c2", limit=10, store=store, map_config=MAP_CONFIG)
    assert error.value.status_code == 409
    store.read_layer.assert_not_called()
