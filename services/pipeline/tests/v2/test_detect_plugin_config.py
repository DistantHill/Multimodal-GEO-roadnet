"""Plugin configuration is validated before computation."""
from copy import deepcopy
from pathlib import Path
import tomllib
import pytest
from track.v2.config.pipeline_config_loader import load_pipeline_payload
from track.v2.config.migrate_detect import convert_legacy_detect
from track.v2.errors import ConfigError
from track.v2.plugins.core import CandidatePlugin
from track.v2.plugins.discovery import discover

BASE = Path(__file__).resolve().parents[2] / "config"

def payload():
    return tomllib.loads((BASE / "pipeline.toml").read_text())

def load(raw):
    return load_pipeline_payload(raw, base_directory=BASE, postgres_password="test")

def test_multiple_instances_and_unselected_algorithm_are_preserved():
    raw = payload()
    table = raw["detect"]["candidate"]["turn_threehold"]
    table["params"]["strict"] = {**table["params"]["default"], "min_turn_deg": 60.0}
    config = load(raw).detect
    assert config.candidates == ("g_star", "cross")
    assert config.candidate["turn_threehold"].output_instances == ("default",)
    assert config.candidate["turn_threehold"].params["strict"].min_turn_deg == 60.0

@pytest.mark.parametrize("change,match", [
    (lambda d: d.update(candidates=[]), "candidates"),
    (lambda d: d.update(candidates=["missing"]), "unknown algorithm"),
    (lambda d: d.update(candidates=["cross", "cross"]), "duplicate"),
    (lambda d: d.update(cluster_algorithm="missing"), "unknown algorithm"),
    (lambda d: d["candidate"]["cross"].update(output_instances=[]), "output_instances"),
    (lambda d: d["candidate"]["cross"].update(output_instances=["missing"]), "unknown instance"),
    (lambda d: d["candidate"]["cross"]["params"]["default"].update(unexpected=1), "unknown key"),
    (lambda d: d["candidate"]["cross"]["params"]["default"].update(cross_track_task_threads=True), "expected"),
    (lambda d: d["candidate"]["cross"]["params"]["default"].pop("cross_track_task_threads"), "missing required"),
    (lambda d: d.update(use_turn_events=True), "unknown key"),
])
def test_invalid_plugin_config(change, match):
    raw = payload()
    change(raw["detect"])
    with pytest.raises(ConfigError, match=match):
        load(raw)

def test_legacy_conversion_preserves_history():
    current = payload()["detect"]
    legacy = {key: value for key, value in current.items() if key not in {"candidates", "candidate", "cluster_algorithm", "cluster"}}
    legacy.update(current["candidate"]["g_star"]["params"]["default"])
    legacy.update(current["candidate"]["cross"]["params"]["default"])
    legacy.update(use_turn_events=True, use_cross_track_candidates=True,
                  candidate_dbscan_eps_m=100.0, candidate_dbscan_min_samples=3)
    history = deepcopy(legacy)
    converted = convert_legacy_detect(legacy, turn_algorithm="g_star", cluster_algorithm="dbscan")
    assert legacy == history
    raw = payload()
    raw["detect"] = converted
    assert load(raw).detect.candidates == ("g_star", "cross")
    legacy.pop("turn_window")
    with pytest.raises(ConfigError, match="missing required"):
        convert_legacy_detect(legacy, turn_algorithm="g_star", cluster_algorithm="dbscan")

def test_discovery_rejects_unsupported_point_output(tmp_path, monkeypatch):
    package = tmp_path / "example_plugins"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "invalid.py").write_text('from dataclasses import dataclass\nfrom track.v2.plugins.core import CandidatePlugin\n@dataclass\nclass Params:\n    threshold: float\nPLUGIN=CandidatePlugin("test",Params,"candidate_segments",lambda *a: None)\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ConfigError, match="unsupported"):
        discover("example_plugins", CandidatePlugin)


def test_discovery_rejects_duplicate_algorithm_ids(tmp_path, monkeypatch):
    package = tmp_path / "duplicate_plugins"
    package.mkdir()
    (package / "__init__.py").write_text("")
    source = ('from dataclasses import dataclass\nfrom track.v2.plugins.core import CandidatePlugin\n'
              '@dataclass\nclass Params:\n    threshold: float\n'
              'PLUGIN=CandidatePlugin("same",Params,"candidate_points",lambda *a: None)\n')
    for name in ("first", "second"):
        (package / (name + ".py")).write_text(source)
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ConfigError, match="duplicate plugin ID"):
        discover("duplicate_plugins", CandidatePlugin)
