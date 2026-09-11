from unittest.mock import Mock, patch
from contextlib import contextmanager

import pytest
import psycopg
from fastapi import HTTPException

from backend.map_api import PostgresMapStore
from backend.map_schemas import ClipRequest, ReplayRequest


@pytest.mark.parametrize("operation", ["replay", "clip"])
@pytest.mark.parametrize("exists,code", [(False, "database_not_found")])
def test_wrong_database_rejected_before_spatial_queries(operation, exists, code):
    store = PostgresMapStore(Mock())
    store._fetchone = Mock(return_value=("roadnet", exists))
    name = "other' database"
    request = (
        ReplayRequest(database_name=name, source_row_keys=["1"])
        if operation == "replay"
        else ClipRequest(database_name=name, detect_id="12345678-1234-5678-1234-567812345678", intersection_ids=[1, 2])
    )
    with pytest.raises(HTTPException) as error:
        getattr(store, operation)(request)
    assert error.value.status_code == (409 if exists else 404)
    assert error.value.detail["error"] == code
    assert "请确认 QGIS 图层的 database 是否对应" in error.value.detail["message"]
    sql, params = store._fetchone.call_args.args
    assert name not in sql
    assert params == (name,)


def test_matching_database_and_legacy_web_request():
    store = PostgresMapStore(Mock())
    store._fetchone = Mock(return_value=("roadnet", True))
    store._validate_database("roadnet")
    store._validate_database(None)
    assert store._fetchone.call_count == 1


@pytest.mark.parametrize("operation", ["replay", "clip"])
def test_existing_database_routes_request_without_changing_default(operation):
    store = PostgresMapStore(Mock())
    store._fetchone = Mock(return_value=("roadnet", True))
    target_connection = Mock()
    closed = []

    @contextmanager
    def connection(name):
        assert name == "other' database"
        try:
            yield target_connection
        finally:
            closed.append(True)

    request = (
        ReplayRequest(database_name="other' database", source_row_keys=["1"])
        if operation == "replay" else ClipRequest(
            database_name="other' database", detect_id="12345678-1234-5678-1234-567812345678",
            intersection_ids=[1, 2],
        )
    )
    with patch("backend.map_api.open_read_connection", connection), patch(
        "backend.map_api.require_schema_v3"
    ) as schema, patch("backend.map_api.PostgresMapStore") as factory:
        getattr(factory.return_value, operation).return_value = {"features": []}
        result = store._query_database(operation, request)
    schema.assert_called_once_with(target_connection)
    factory.assert_called_once_with(target_connection)
    forwarded = getattr(factory.return_value, operation).call_args.args[0]
    assert forwarded.database_name is None
    assert request.database_name == "other' database"
    assert result["metadata"]["database_name"] == request.database_name
    assert closed == [True]


def test_incompatible_target_schema_returns_structured_error():
    store = PostgresMapStore(Mock())
    store._fetchone = Mock(return_value=("roadnet", True))
    with patch("backend.map_api.open_read_connection"), patch(
        "backend.map_api.require_schema_v3", side_effect=RuntimeError("bad schema")
    ), pytest.raises(HTTPException) as error:
        store.replay(ReplayRequest(database_name="other", source_row_keys=["1"]))
    assert error.value.status_code == 409
    assert error.value.detail["error"] == "database_schema_mismatch"


def test_target_connection_error_is_sanitized():
    store = PostgresMapStore(Mock())
    store._fetchone = Mock(return_value=("roadnet", True))
    with patch("backend.map_api.open_read_connection", side_effect=psycopg.OperationalError(
        "sensitive connection details"
    )), pytest.raises(HTTPException) as error:
        store.replay(ReplayRequest(database_name="other", source_row_keys=["1"]))
    assert error.value.status_code == 503
    assert error.value.detail["error"] == "database_query_failed"
    assert "sensitive" not in str(error.value.detail)
