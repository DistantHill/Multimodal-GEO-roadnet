from pathlib import Path
from unittest.mock import patch

from pipeline.__main__ import PipelineConfig, check_config, main
from track.v2.config import default_postgres_config


def test_pipeline_config_defaults(monkeypatch) -> None:
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.delenv("PARQUET_ROOT", raising=False)

    config = PipelineConfig.from_environment()

    assert config.data_root == Path("/data")
    assert config.parquet_root == Path("/data/parquet")
    assert config.duckdb_root == Path("/data/duckdb")
    assert config.polars_root == Path("/data/polars")


def test_parquet_default_follows_data_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("PARQUET_ROOT", raising=False)

    config = PipelineConfig.from_environment()

    assert config.parquet_root == tmp_path / "parquet"


def test_explicit_parquet_root_can_be_independent(monkeypatch, tmp_path) -> None:
    data_root = tmp_path / "data"
    parquet_root = tmp_path / "external-parquet"
    monkeypatch.setenv("DATA_ROOT", str(data_root))
    monkeypatch.setenv("PARQUET_ROOT", str(parquet_root))

    config = PipelineConfig.from_environment()

    assert check_config(config) == 0
    assert all(path.is_dir() for path in (
        data_root, parquet_root, data_root / "duckdb", data_root / "polars",
    ))


def test_check_config_rejects_relative_root(monkeypatch) -> None:
    monkeypatch.setenv("DATA_ROOT", "relative")
    monkeypatch.delenv("PARQUET_ROOT", raising=False)

    assert check_config(PipelineConfig.from_environment()) == 1


def test_check_config_rejects_derived_root_symlink_escape(monkeypatch, tmp_path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "duckdb").symlink_to(tmp_path / "outside", target_is_directory=True)
    monkeypatch.setenv("DATA_ROOT", str(data_root))
    monkeypatch.delenv("PARQUET_ROOT", raising=False)

    assert check_config(PipelineConfig.from_environment()) == 1


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
