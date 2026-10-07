"""Exercise real FastAPI dependency entry and exit, including connection failures."""

from contextlib import contextmanager
from unittest.mock import Mock

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import database
from backend.api import map_routes
from backend.map_errors import MapDatabaseNotFoundError


@pytest.fixture
def client(backend_config):
    app = FastAPI()
    app.state.config = backend_config
    app.include_router(map_routes.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.mark.parametrize("phase", ["connect", "initialize", "query"])
def test_connection_and_query_errors_return_sanitized_503(client, monkeypatch, phase):
    connection = Mock()
    error = psycopg.OperationalError("sensitive connection details")
    connect = Mock(return_value=connection)
    if phase == "connect":
        connect.side_effect = error
    elif phase == "initialize":
        connection.execute.side_effect = error
    else:
        monkeypatch.setattr(map_routes.PostgresMapStore, "layer_catalog", Mock(side_effect=error))
    monkeypatch.setattr(database.psycopg, "connect", connect)

    response = client.get("/api/v1/map/layers")

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "database_query_failed"
    assert "sensitive" not in response.text
    if phase != "connect":
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()


@pytest.mark.parametrize("operation", ["layers", "replay", "clip"])
@pytest.mark.parametrize("selected", [None, "roadnet", "other"])
def test_selected_database_holds_one_connection_and_preserves_body_metadata(
    client, monkeypatch, operation, selected,
):
    opened, active, validated = [], [], []

    @contextmanager
    def open_connection(name=None):
        name = name or "roadnet"
        assert not active
        opened.append(name)
        active.append(name)
        try:
            yield name
        finally:
            active.pop()

    def validate(store, name):
        assert store.connection == "roadnet"
        validated.append(name)

    def result(store, request=None):
        assert active == [selected or "roadnet"]
        assert store.connection == active[0]
        if request is not None:
            assert request.database_name is None
        return {"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
                "features": [], "metadata": {}}

    monkeypatch.setattr(map_routes, "open_read_connection", open_connection)
    schema = Mock()
    monkeypatch.setattr(map_routes, "require_current_schema", schema)
    monkeypatch.setattr(map_routes.PostgresMapStore, "_validate_database", validate)
    monkeypatch.setattr(map_routes.PostgresMapStore, "layer_catalog" if operation == "layers" else operation, result)

    if operation == "layers":
        response = client.get("/api/v1/map/layers", params={"database_name": selected} if selected else {})
    else:
        body = {"source_row_keys": ["1"]} if operation == "replay" else {
            "detect_id": "12345678-1234-5678-1234-567812345678", "intersection_ids": [1, 2],
        }
        if selected:
            body["database_name"] = selected
        response = client.post(f"/api/v1/map/{operation}", json=body)
        if selected:
            assert response.json()["metadata"]["database_name"] == selected

    assert response.status_code == 200, response.text
    assert opened == (["roadnet", "other"] if selected == "other" else ["roadnet"])
    assert validated == (["other"] if selected == "other" else [])
    assert schema.call_count == int(selected is not None)
    assert active == []


def test_missing_database_closes_catalog_connection_without_opening_target(client, monkeypatch):
    context = Mock()
    monkeypatch.setattr(map_routes, "open_read_connection", Mock(return_value=context))
    context.__enter__ = Mock(return_value=Mock())
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(map_routes.PostgresMapStore, "_validate_database", Mock(
        side_effect=MapDatabaseNotFoundError("missing", "roadnet"),
    ))
    response = client.get("/api/v1/map/layers", params={"database_name": "missing"})
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "database_not_found"
    map_routes.open_read_connection.assert_called_once_with()
    context.__exit__.assert_called_once()


@pytest.mark.parametrize("body_selection", [False, True])
def test_schema_mismatch_closes_connection_and_preserves_error_shape(client, monkeypatch, body_selection):
    connection = Mock()
    monkeypatch.setattr(database.psycopg, "connect", Mock(return_value=connection))
    monkeypatch.setattr(map_routes, "require_current_schema", Mock(side_effect=RuntimeError("stale")))
    if body_selection:
        response = client.post("/api/v1/map/replay", json={"database_name": "roadnet", "source_row_keys": ["1"]})
        assert response.json()["detail"]["error"] == "database_schema_mismatch"
    else:
        response = client.get("/api/v1/map/layers", params={"database_name": "roadnet"})
        assert isinstance(response.json()["detail"], str)
    assert response.status_code == 409
    connection.rollback.assert_called_once()
    connection.close.assert_called_once()
