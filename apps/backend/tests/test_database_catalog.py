from unittest.mock import Mock, patch, MagicMock

import pytest
from fastapi import HTTPException
import psycopg

from backend.api.database_catalog import databases, tables, table_counts


def request():
    value = Mock()
    value.app.state.config.map.catalog_exact_count_max_rows = 50_000
    return value


def test_database_list():
    connection = Mock()
    connection.execute.return_value.fetchall.return_value = [("roadnet",), ("test",)]
    connection.execute.return_value.fetchone.return_value = ("roadnet",)
    assert databases(connection) == {"databases": ["roadnet", "test"], "default_database": "roadnet"}


def test_tables_use_statistics_not_counts():
    connection = Mock()
    target = MagicMock()
    target.__enter__.return_value.execute.return_value.fetchall.return_value = [
        ("public", "points", 50000, "r"), ("other", "points", 80000, "r"),
    ]
    with patch("backend.api.database_catalog.open_read_connection", return_value=target) as opened:
        result = tables(request(), "test' name", connection)
    opened.assert_called_once_with("test' name")
    assert result["row_count_is_estimate"] is True
    assert result["tables"][0]["estimated_rows"] == 50000
    assert result["tables"][1]["estimated_rows"] == 80000
    sql = target.__enter__.return_value.execute.call_args.args[0]
    assert "reltuples" in sql and "COUNT(" not in sql
    assert connection.execute.call_args.args[1] == ("test' name",)


@pytest.mark.parametrize("estimate", [-1, 0, 49999])
def test_small_or_unknown_tables_get_exact_count(estimate):
    target = MagicMock()
    target.execute.return_value.fetchone.return_value = (0,)
    result = table_counts(
        target, [('odd"schema', 'odd"table', estimate, "r")],
        exact_count_max_rows=50_000,
    )[0]
    assert result["row_count"] == 0
    assert result["row_count_is_estimate"] is False
    assert target.execute.call_args.args[0].as_string() == 'SELECT COUNT(*) FROM "odd""schema"."odd""table"'


def test_count_failure_keeps_estimate_and_continues():
    target = MagicMock()
    cursor = Mock()
    cursor.fetchone.return_value = (7,)
    target.execute.side_effect = [psycopg.errors.QueryCanceled("timeout"), cursor]
    result = table_counts(
        target, [("public", "a", -1, "r"), ("public", "b", 4, "r")],
        exact_count_max_rows=50_000,
    )
    assert result[0]["row_count"] is None
    assert result[0]["count_error"]
    assert result[1]["row_count"] == 7
    assert result[1]["row_count_is_estimate"] is False


def test_missing_database():
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = None
    with pytest.raises(HTTPException) as error:
        tables(request(), "missing", connection)
    assert error.value.status_code == 404


def test_connection_error_does_not_expose_credentials():
    with patch("backend.api.database_catalog.open_read_connection", side_effect=psycopg.OperationalError("secret")):
        with pytest.raises(HTTPException) as error:
            tables(request(), "denied", Mock())
    assert error.value.status_code == 503
    assert "secret" not in str(error.value.detail)
