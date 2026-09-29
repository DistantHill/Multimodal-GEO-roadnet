"""Strict Pipeline TOML loader tests."""
from __future__ import annotations

from dataclasses import MISSING, fields, replace
from pathlib import Path

import pytest

from .config_factory import pipeline_config
from track.v2.config import PostgresConfig, load_pipeline_config
from track.v2.errors import ConfigError
from track.v2.intersections.detect_intersections import IntersectConfig
from track.v2.models import (ClipConfig, FieldConfig, FieldSpec, FittingConfig, NetworkConfig,
                             ProjectionConfig, RefineConfig, RuntimeConfig)


CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "pipeline.toml"
FIELD_PATH = CONFIG_PATH.with_name("field_specs.toml")
CONFIG_TYPES = (PostgresConfig, ProjectionConfig, RefineConfig, IntersectConfig,
                ClipConfig, FittingConfig, NetworkConfig, RuntimeConfig, FieldConfig, FieldSpec)


def _config_text() -> str:
    return CONFIG_PATH.read_text(encoding="utf-8").replace(
        'field_specs_path = "field_specs.toml"',
        f'field_specs_path = "{FIELD_PATH}"',
    )


def _write(tmp_path: Path, transform=lambda text: text) -> Path:
    path = tmp_path / "pipeline.toml"
    path.write_text(transform(_config_text()), encoding="utf-8")
    return path


def test_checked_in_complete_config_loads() -> None:
    config = pipeline_config()
    assert config.projection.processing_srid == 32649
    assert config.runtime.duckdb_threads == 4
    assert config.runtime.duckdb_memory_limit == "1GB"
    assert config.runtime.duckdb_event_memory_limit == "4GB"
    assert config.runtime.duckdb_grid_memory_limit == "4GB"
    assert config.detect.cross_track_task_grid_m == 5000.0
    assert config.detect.cross_track_task_threads == 4
    assert config.detect.candidate_dbscan_eps_m == 100.0
    assert config.detect.candidate_dbscan_min_samples == 3
    assert len(config.parser.fields.specs) == 9


def test_smoke_config_and_optional_event_limit(tmp_path: Path) -> None:
    smoke = load_pipeline_config(CONFIG_PATH.with_name("pipeline-roadnet-test.toml"), postgres_password="secret")
    assert smoke.runtime.duckdb_memory_limit == "1GB"
    assert smoke.runtime.duckdb_event_memory_limit == "4GB"
    assert smoke.runtime.duckdb_grid_memory_limit == "4GB"
    legacy = _write(tmp_path, lambda text: "\n".join(
        line for line in text.splitlines() if not line.startswith(("duckdb_event_memory_limit =", "duckdb_grid_memory_limit ="))
    ))
    loaded = load_pipeline_config(legacy, postgres_password="secret")
    assert loaded.runtime.duckdb_event_memory_limit == loaded.runtime.duckdb_memory_limit == "1GB"
    assert loaded.runtime.duckdb_grid_memory_limit == "1GB"


def test_debug_config_uses_joint_candidate_clustering() -> None:
    debug = load_pipeline_config(CONFIG_PATH.with_name("pipeline-debug.toml"), postgres_password="secret")
    assert debug.postgres.database == "roadnet_test"
    assert debug.detect.candidate_dbscan_eps_m == 100.0
    assert debug.detect.candidate_dbscan_min_samples == 3


@pytest.mark.parametrize("value", ["0.0", "-1.0", "inf", "nan"])
def test_detect_task_grid_size_requires_finite_positive_number(tmp_path: Path, value: str) -> None:
    path = _write(tmp_path, lambda text: text.replace(
        "cross_track_task_grid_m = 5000.0", f"cross_track_task_grid_m = {value}",
    ))
    with pytest.raises(ConfigError, match="detect.cross_track_task_grid_m"):
        load_pipeline_config(path, postgres_password="secret")


@pytest.mark.parametrize("value", ["0.0", "-1.0", "inf", "nan"])
def test_joint_candidate_dbscan_radius_requires_finite_positive_number(tmp_path: Path, value: str) -> None:
    path = _write(tmp_path, lambda text: text.replace(
        "candidate_dbscan_eps_m = 100.0", f"candidate_dbscan_eps_m = {value}",
    ))
    with pytest.raises(ConfigError, match="detect.candidate_dbscan_eps_m"):
        load_pipeline_config(path, postgres_password="secret")


@pytest.mark.parametrize("value", ["0", "-1"])
def test_joint_candidate_dbscan_min_samples_requires_positive_integer(tmp_path: Path, value: str) -> None:
    path = _write(tmp_path, lambda text: text.replace(
        "candidate_dbscan_min_samples = 3", f"candidate_dbscan_min_samples = {value}",
    ))
    with pytest.raises(ConfigError, match="detect.candidate_dbscan_min_samples"):
        load_pipeline_config(path, postgres_password="secret")


@pytest.mark.parametrize("value", ["0", "9", "true"])
def test_detect_task_threads_requires_integer_between_one_and_eight(
    tmp_path: Path, value: str,
) -> None:
    path = _write(tmp_path, lambda text: text.replace(
        "cross_track_task_threads = 4", f"cross_track_task_threads = {value}",
    ))
    with pytest.raises(ConfigError, match="detect.cross_track_task_threads"):
        load_pipeline_config(path, postgres_password="secret")


def test_tunable_dataclasses_have_no_python_defaults() -> None:
    context_fields = {"query_active_from_db", "db_table", "kept_table", "source_table",
                      "refine_id", "live_status"}
    for cls in CONFIG_TYPES:
        for item in fields(cls):
            if cls is IntersectConfig and item.name in context_fields:
                continue
            assert item.default is MISSING and item.default_factory is MISSING, (
                f"{cls.__name__}.{item.name} owns a Python default"
            )


def test_paths_are_resolved_relative_to_main_toml(tmp_path: Path) -> None:
    assert pipeline_config().logging.directory == Path("/data/logs")
    path = _write(tmp_path, lambda text: text.replace(
        'directory = "/data/logs"', 'directory = "../generated"',
    ))
    config = load_pipeline_config(path, postgres_password="secret")
    assert config.logging.directory == (path.parent / "../generated").resolve()


def test_unknown_missing_type_and_range_errors_are_aggregated(tmp_path: Path) -> None:
    path = _write(tmp_path, lambda text: text
        .replace("[refine]", "[refine]\nunknown_value = 1")
        .replace("max_gap_s = 120.0", "# max_gap_s removed")
        .replace("duckdb_threads = 4", 'duckdb_threads = "many"')
        .replace("port = 5432", "port = 70000"))
    with pytest.raises(ConfigError) as raised:
        load_pipeline_config(path, postgres_password="secret")
    message = str(raised.value)
    assert "refine.unknown_value: unknown key" in message
    assert "refine.max_gap_s: missing required key" in message
    assert "runtime.duckdb_threads: expected" in message
    assert "postgres.port: must be in [1, 65535]" in message


@pytest.mark.parametrize("password", ["", "change-me"])
def test_password_is_required_and_placeholder_is_rejected(password: str) -> None:
    with pytest.raises(ConfigError, match="POSTGRES_PASSWORD"):
        load_pipeline_config(CONFIG_PATH, postgres_password=password)


def test_cross_field_errors_are_aggregated(tmp_path: Path) -> None:
    path = _write(tmp_path, lambda text: text
        .replace("min_turn_deg = 45.0", "min_turn_deg = 175.0")
        .replace("min_heading_delta_deg = 80.0", "min_heading_delta_deg = 120.0")
        .replace("max_heading_delta_deg = 100.0", "max_heading_delta_deg = 90.0"))
    with pytest.raises(ConfigError) as raised:
        load_pipeline_config(path, postgres_password="secret")
    message = str(raised.value)
    assert "detect.min_turn_deg" in message
    assert "refine.min_heading_delta_deg" in message


def test_loaded_config_is_immutable() -> None:
    config = pipeline_config()
    with pytest.raises(Exception):
        config.runtime = replace(config.runtime, duckdb_threads=1)
