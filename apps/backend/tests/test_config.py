from __future__ import annotations

from pathlib import Path

import pytest

from backend.config import ConfigError, load_backend_config


CONFIG = """
[postgres]
host = "postgres"
port = 5432
database = "roadnet"
user = "roadnet"
connect_timeout_seconds = 5
statement_timeout_ms = 10000
[qdrant]
host = "qdrant"
port = 6333
[map]
layer_limit_default = 1000
layer_limit_max = 5000
catalog_exact_count_max_rows = 50000
replay_max_vehicles_default = 25
replay_max_vehicles_max = 100
replay_max_points_default = 10000
replay_max_points_max = 50000
replay_selection_ids_max = 1000
clip_max_vehicles_default = 50
clip_max_vehicles_max = 200
clip_max_points_default = 20000
clip_max_points_max = 100000
clip_feature_limit = 1000
[health]
postgres_timeout_seconds = 2
qdrant_timeout_seconds = 2.0
[runtime]
title = "Roadnet Backend"
version = "0.3.0"
"""


def write_config(tmp_path: Path, text: str = CONFIG) -> Path:
    path = tmp_path / "backend.toml"
    path.write_text(text)
    return path


def test_loads_complete_config_and_excludes_password_from_snapshot(tmp_path: Path) -> None:
    config = load_backend_config(
        write_config(tmp_path), environ={"POSTGRES_PASSWORD": "secret"}
    )
    assert config.postgres.statement_timeout_ms == 10_000
    assert config.map.layer_limit_max == 5_000
    assert "secret" not in str(config.safe_snapshot())
    assert len(config.digest()) == 64


def test_digest_excludes_source_path(tmp_path: Path) -> None:
    first_path = tmp_path / "first"
    first_path.mkdir()
    first = load_backend_config(
        write_config(first_path), environ={"POSTGRES_PASSWORD": "secret"}
    )
    second_path = tmp_path / "second"
    second_path.mkdir()
    second = load_backend_config(
        write_config(second_path), environ={"POSTGRES_PASSWORD": "secret"}
    )
    assert first.source_path != second.source_path
    assert first.safe_snapshot() == second.safe_snapshot()
    assert first.digest() == second.digest()


def test_reports_all_schema_and_range_errors(tmp_path: Path) -> None:
    invalid = CONFIG.replace("port = 5432", "port = 70000", 1).replace(
        "layer_limit_default = 1000", "layer_limit_default = 6000"
    ).replace("version = \"0.3.0\"", "version = \"0.3.0\"\nunknown = 1")
    with pytest.raises(ConfigError) as caught:
        load_backend_config(write_config(tmp_path, invalid), environ={})
    message = str(caught.value)
    assert "postgres.port" in message
    assert "map.layer_limit_default" in message
    assert "runtime.unknown" in message
    assert "POSTGRES_PASSWORD" in message


def test_rejects_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unable to read Backend config"):
        load_backend_config(tmp_path / "missing.toml", environ={"POSTGRES_PASSWORD": "x"})
