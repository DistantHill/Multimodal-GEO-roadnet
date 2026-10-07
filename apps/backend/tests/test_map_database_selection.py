from unittest.mock import Mock, patch
from contextlib import contextmanager

import pytest
import psycopg
from fastapi import HTTPException

from backend.api.map_routes import database_query_error, get_map_store
from backend.map_errors import MapDatabaseNotFoundError, MapSchemaMismatchError
from backend.stores.map_layers import PostgresMapStore
from backend.api.map_contracts import ClipRequest, ReplayRequest


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
    with pytest.raises(MapDatabaseNotFoundError) as error:
        getattr(store, operation)(request)
    assert "请确认 QGIS 图层的 database 是否对应" in str(error.value)
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
    with patch("backend.stores.map_layers.open_read_connection", connection), patch(
        "backend.stores.map_layers.require_current_schema"
    ) as schema, patch("backend.stores.map_layers.PostgresMapStore") as factory:
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
    with patch("backend.stores.map_layers.open_read_connection"), patch(
        "backend.stores.map_layers.require_current_schema", side_effect=RuntimeError("bad schema")
    ), pytest.raises(MapSchemaMismatchError) as error:
        store.replay(ReplayRequest(database_name="other", source_row_keys=["1"]))
    assert error.value.detail["error"] == "database_schema_mismatch"


def test_target_connection_error_is_sanitized_by_route_dependency():
    with patch("backend.api.map_routes.open_read_connection"):
        dependency = get_map_store(database_name=None)
        next(dependency)
        with pytest.raises(HTTPException) as error:
            dependency.throw(psycopg.OperationalError("sensitive connection details"))
    assert error.value.status_code == 503
    assert error.value.detail["error"] == "database_query_failed"
    assert "sensitive" not in str(error.value.detail)


@pytest.mark.parametrize("sqlstate,primary,expected", [
    ("53100", 'could not resize shared memory segment: No space left on device', "共享内存"),
    ("53100", 'could not extend file', "存储空间"),
    ("53200", 'out of memory', "可用内存"),
])
def test_resource_errors_report_sqlstate_without_query(sqlstate, primary, expected):
    error = Mock(sqlstate=sqlstate)
    error.diag.message_primary = primary
    result = database_query_error(error)
    assert result.status_code == 503
    assert result.detail["error"] == "database_resource_exhausted"
    assert result.detail["sqlstate"] == sqlstate
    assert expected in result.detail["message"]
    assert sqlstate in result.detail["message"]
