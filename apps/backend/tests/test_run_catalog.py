from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from backend.map_api import run_catalog, read_layer


def test_run_catalog_preserves_parent_relationships():
    store = Mock()
    timestamp = "2026-09-11T12:00:00+00:00"
    store._fetchall.side_effect = [[("c1", timestamp)], [("d1", "c1", timestamp)], [("f1", "d1", timestamp)]]
    result = run_catalog(store)
    assert result == {"clean": [{"id": "c1", "started_at": timestamp}],
                      "detect": [{"id": "d1", "clean_id": "c1", "started_at": timestamp}],
                      "fit": [{"id": "f1", "detect_id": "d1", "started_at": timestamp}]}
    assert all("status = 'completed'" in call.args[0] for call in store._fetchall.call_args_list)


def test_layer_rejects_mismatched_clean_before_loading():
    store = Mock()
    store._fetchone.return_value = None
    with pytest.raises(HTTPException) as error:
        read_layer("intersections", bbox=None, detect_id="d1", fit_id=None, clean_id="c2", limit=10, store=store)
    assert error.value.status_code == 409
    store.read_layer.assert_not_called()
