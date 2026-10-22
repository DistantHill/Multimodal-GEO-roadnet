from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

import psycopg
import pytest
from shapely import from_wkb

from pipeline.database import publish


class FakeTransaction:
    def __init__(self, connection: FakeConnection):
        self.connection = connection

    def __enter__(self) -> FakeTransaction:
        self.before = deepcopy(self.connection.rows)
        return self

    def __exit__(self, kind: type | None, exc: BaseException | None, tb: Any) -> None:
        if exc is not None:
            self.connection.rows = self.before
            self.connection.rollbacks += 1
        else:
            self.connection.commits += 1


class FakeConnection:
    def __init__(self, *, fail_second: bool = False, postgis: bool = True):
        self.fail_second, self.postgis = fail_second, postgis
        self.rows: dict[str, list] = {}
        self.statements: list[str] = []
        self.rollbacks = self.commits = 0

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    def cursor(self) -> FakeConnection:
        return self

    def execute(self, statement: Any, params: Any = None) -> None:
        self.statements.append(statement if isinstance(statement, str) else statement.as_string())

    def executemany(self, statement: Any, params: list[tuple]) -> None:
        text = statement.as_string()
        self.statements.append(text)
        layer = "centerlines" if '"centerlines"' in text else "intersections"
        if self.fail_second and layer == "centerlines":
            raise psycopg.errors.UniqueViolation("sensitive_dsn_password must never escape")
        self.rows.setdefault(layer, []).extend(params)

    def fetchone(self) -> tuple[bool]:
        return (self.postgis,)


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("ROADNET_RESULT_DSN", "postgresql://user:sensitive_dsn_password@localhost/roadnet")
    return {"input": {"processing_crs": "EPSG:32650"},
            "database": {"enabled": True, "schema": "roadnet_workbench", "dsn_env": "ROADNET_RESULT_DSN"}}


def features() -> tuple[list[dict], list[dict]]:
    base = {"run_id": "run-a", "refine_id": "refine-a", "detect_id": "detect-a"}
    return ([{"type": "Feature", "id": "intersection-a",
              "properties": {**base, "vehicles": 7, "label": "十字路口"},
              "geometry": {"type": "Point", "coordinates": [500000, 3500000]}}],
            [{"type": "Feature", "id": "centerline-a",
              "properties": {**base, "fit_id": "fit-a", "clip_ids": ["clip-a", "clip-b"]},
              "geometry": {"type": "LineString", "coordinates": [[500000, 3500000], [500050, 3500050]]}}])


def test_only_final_features_written_with_full_attributes_and_metric_conversion(
    config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = FakeConnection()
    monkeypatch.setattr(psycopg, "connect", lambda dsn: connection)
    intersections, centerlines = features()
    result = publish(config, intersections, centerlines)
    assert result == {"status": "PUBLISHED", "schema": "roadnet_workbench", "crs": "EPSG:4326",
                      "tables": {"intersections": 1, "centerlines": 1}}
    assert connection.commits == 1 and connection.rollbacks == 0
    assert set(connection.rows) == {"intersections", "centerlines"}
    intersection = connection.rows["intersections"][0]
    geometry = from_wkb(intersection[-1])
    assert geometry.x == pytest.approx(117.0, abs=0.01)
    assert 31 < geometry.y < 32
    assert intersection[-2].obj["vehicles"] == 7
    assert intersection[-2].obj["label"] == "十字路口"
    assert connection.rows["centerlines"][0][-2].obj["clip_ids"] == ["clip-a", "clip-b"]
    text = "\n".join(connection.statements)
    assert text.count("USING GIST (geom)") == 2
    assert "CREATE EXTENSION" not in text
    assert "TRUNCATE" not in text and "DELETE" not in text
    assert "sensitive_dsn_password" not in json.dumps(result)


def test_collision_rolls_back_previously_written_layer_and_hides_credentials(
    config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = FakeConnection(fail_second=True)
    monkeypatch.setattr(psycopg, "connect", lambda dsn: connection)
    with pytest.raises(RuntimeError, match="existing result primary id") as error:
        publish(config, *features())
    assert connection.rows == {}
    assert connection.rollbacks == 1 and connection.commits == 0
    assert "sensitive_dsn_password" not in str(error.value)
    assert error.value.__suppress_context__


def test_disabled_publisher_does_not_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Disabled database publisher connected")
    monkeypatch.setattr(psycopg, "connect", fail)
    assert publish({"database": {"enabled": False}}, [], []) == {"status": "DISABLED"}


def test_lineage_and_duplicate_ids_rejected_before_connection(config: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid inputs should be rejected before connecting")
    monkeypatch.setattr(psycopg, "connect", fail)
    intersections, centerlines = features()
    with pytest.raises(ValueError, match="Duplicate primary id"):
        publish(config, intersections * 2, centerlines)
    del centerlines[0]["properties"]["fit_id"]
    with pytest.raises(ValueError, match="requires fit_id"):
        publish(config, intersections, centerlines)


def test_postgis_is_required_without_installing_or_mutating_schema(config: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    connection = FakeConnection(postgis=False)
    monkeypatch.setattr(psycopg, "connect", lambda dsn: connection)
    with pytest.raises(ValueError, match="PostGIS must already"):
        publish(config, *features())
    assert connection.rollbacks == 1
    assert all("CREATE" not in text for text in connection.statements)


def test_schema_name_uses_identifier_quoting(config: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    connection = FakeConnection()
    monkeypatch.setattr(psycopg, "connect", lambda dsn: connection)
    config["database"]["schema"] = 'a"; DROP SCHEMA public; --'
    publish(config, *features())
    schema_statement = next(text for text in connection.statements if text.startswith("CREATE SCHEMA"))
    assert schema_statement == 'CREATE SCHEMA IF NOT EXISTS "a""; DROP SCHEMA public; --"'
