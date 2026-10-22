from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from pipeline.config import DEFAULTS, load_config, validate_config


def base_config() -> dict:
    config = deepcopy(DEFAULTS)
    config["input"]["path"] = "input.parquet"
    return config


def test_single_config_resolves_all_paths_against_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "settings" / "pipeline.toml"
    source.parent.mkdir()
    source.write_text('''
version = 1
[input]
path = "../gps/*.parquet"
[output]
root = "../runs"
[labels.intersections]
path = "../labels/truth.gpkg"
layer = "intersections"
''')
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    config = load_config(source)
    assert Path(config["input"]["path"]).resolve() == tmp_path / "gps" / "*.parquet"
    assert Path(config["output"]["root"]).resolve() == tmp_path / "runs"
    assert Path(config["labels"]["intersections"]["path"]).resolve() == tmp_path / "labels" / "truth.gpkg"


@pytest.mark.parametrize("crs", ["EPSG:4326", "EPSG:2227"])
def test_processing_geometry_requires_metre_projected_crs(crs: str) -> None:
    config = base_config()
    config["input"]["processing_crs"] = crs
    with pytest.raises(ValueError, match="projected CRS with metre axes"):
        validate_config(config)


def test_rejects_unknown_parameters_instead_of_silently_ignoring() -> None:
    config = base_config()
    config["detect"]["min_trun_deg"] = 30
    with pytest.raises(ValueError, match="Unknown configuration key: detect.min_trun_deg"):
        validate_config(config)


@pytest.mark.parametrize("section,key,value", [
    ("resources", "batch_rows", True),
    ("resources", "soft_memory_mb", float("nan")),
    ("refine", "max_gap_s", -1.0),
    ("detect", "cluster_eps_m", 0.0),
    ("refine", "min_subtrack_points", 1),
])
def test_invalid_operational_parameters_fail_early(section: str, key: str, value: object) -> None:
    config = base_config()
    config[section][key] = value
    with pytest.raises(ValueError):
        validate_config(config)


def test_duckdb_budget_leaves_room_for_native_operators() -> None:
    config = base_config()
    config["resources"].update(soft_memory_mb=512, duckdb_memory_mb=512)
    with pytest.raises(ValueError, match="leave memory"):
        validate_config(config)


def test_angle_ranges_are_consistent() -> None:
    config = base_config()
    config["detect"].update(min_turn_deg=100.0, max_turn_deg=90.0)
    with pytest.raises(ValueError, match="min_turn_deg"):
        validate_config(config)


def test_validation_does_not_mutate_caller_or_defaults() -> None:
    config = base_config()
    original = deepcopy(config)
    result = validate_config(config)
    result["refine"]["max_gap_s"] = 9999
    assert config == original
    assert DEFAULTS["refine"]["max_gap_s"] != 9999
