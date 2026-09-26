from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

from pipeline.__main__ import check_config, main
from track.v2.config import load_pipeline_config


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config/pipeline-roadnet-test.toml"


def pipeline_config():
    return load_pipeline_config(CONFIG_PATH, postgres_password="test-password")


def test_repository_config_owns_runtime_paths() -> None:
    config = pipeline_config()
    assert config.paths.data_root == Path("/data")
    assert config.paths.parquet_directory == Path("/data/parquet")
    assert config.postgres.host == "postgres"


def test_snapshot_is_stable_and_excludes_database_password() -> None:
    config = pipeline_config()

    first = config.snapshot_parameters()
    second = config.snapshot_parameters()

    assert first == second
    assert len(first["config_digest"]) == 64
    serialized = json.dumps(first, sort_keys=True)
    assert "test-password" not in serialized
    assert "password" not in serialized
    assert first["semantic_config"]["projection"]["processing_srid"] == 32649


def test_check_config_creates_configured_directories(tmp_path) -> None:
    config = pipeline_config()
    paths = replace(
        config.paths,
        data_root=tmp_path / "data",
        parquet_directory=tmp_path / "parquet",
    )
    config = replace(
        config,
        paths=paths,
        logging=replace(config.logging, directory=tmp_path / "logs"),
    )
    assert check_config(config) == 0
    assert all(path.is_dir() for path in (
        paths.data_root,
        paths.parquet_directory,
        config.logging.directory,
    ))


def test_check_config_rejects_files_as_runtime_directories(tmp_path) -> None:
    config = pipeline_config()
    invalid = tmp_path / "not-a-directory"
    invalid.write_text("x", encoding="utf-8")
    config = replace(config, paths=replace(config.paths, data_root=invalid))
    assert check_config(config) == 1


def test_run_subcommand_forwards_help_to_original_cli(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["pipeline", "run", "--help"])
    with patch("track.v2.cli.run_pipeline.main", return_value=0) as delegated:
        assert main() == 0
    delegated.assert_called_once_with()
    assert __import__("sys").argv == ["track.v2.cli.run_pipeline", "--help"]


def test_artifacts_subcommand_forwards_to_artifact_cli(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["pipeline", "artifacts", "reconcile"])
    with patch("track.v2.cli.reconcile_artifacts.main", return_value=0) as delegated:
        assert main() == 0
    delegated.assert_called_once_with()
    assert __import__("sys").argv == ["track.v2.cli.reconcile_artifacts", "reconcile"]
