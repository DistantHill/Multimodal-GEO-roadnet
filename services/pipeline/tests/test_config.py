from pathlib import Path
from unittest.mock import patch

from pipeline.__main__ import PipelineConfig, main
from track.v2.config import default_postgres_config


def test_pipeline_config_defaults(monkeypatch) -> None:
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.delenv("PARQUET_ROOT", raising=False)

    config = PipelineConfig.from_environment()

    assert config.data_root == Path("/data")
    assert config.parquet_root == Path("/data/parquet")


def test_postgres_config_accepts_compose_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_DB", "roadnet")
    monkeypatch.setenv("POSTGRES_USER", "roadnet")
    monkeypatch.setenv("POSTGRES_PASSWORD", "local-test-password")

    config = default_postgres_config(tmp_path / "missing.env")

    assert config.host == "postgres"
    assert config.database == "roadnet"
    assert config.user == "roadnet"
    assert config.password == "local-test-password"


def test_legacy_track_environment_takes_precedence(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    monkeypatch.setenv("TRACK_PG_HOST", "legacy-db")

    config = default_postgres_config(tmp_path / "missing.env")

    assert config.host == "legacy-db"


def test_run_subcommand_forwards_help_to_original_cli(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["pipeline", "run", "--help"])

    with patch("track.v2.cli.run.main", return_value=0) as delegated:
        assert main() == 0

    delegated.assert_called_once_with()
    assert __import__("sys").argv == ["track.v2.cli.run", "--help"]


def test_cv_preview_subcommand_forwards_help(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["pipeline", "cv-preview", "--help"])
    with patch("track.v2.cli.cv_preview.main", return_value=0) as delegated:
        assert main() == 0
    delegated.assert_called_once_with()
    assert __import__("sys").argv == ["track.v2.cli.cv_preview", "--help"]
