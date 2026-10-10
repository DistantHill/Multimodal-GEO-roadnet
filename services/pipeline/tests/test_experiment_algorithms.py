"""Workspace algorithm selection, snapshots and restore stay experiment-scoped."""
import tomllib

from track.v2.cli.experiment import toml_text
from track.v2.experiments.config import load_workspace, refine_key
from test_experiment_runner import workspace, setup_stages
from track.v2.experiments import runner


def test_named_algorithms_share_refine_and_have_distinct_snapshots(tmp_path, monkeypatch):
    original = workspace(tmp_path)
    payload = original.payload
    payload["experiment"]["schemes"] = ["turn", "turn_threehold", "hdbscan"]
    import copy
    angle = copy.deepcopy(payload["detect"])
    angle["candidates"] = ["turn_threehold"]
    density = copy.deepcopy(angle)
    density["cluster_algorithm"] = "hdbscan"
    density["cluster"]["hdbscan"] = copy.deepcopy(density["cluster"]["dbscan"])
    payload["schemes"]["turn_threehold"] = {"detect": angle}
    payload["schemes"]["hdbscan"] = {"detect": density}
    text = toml_text(payload)
    assert tomllib.loads(text) == payload
    original.config_path.write_text(text)
    loaded = load_workspace(original.config_path, password="test")
    assert loaded.configs["turn"].detect.candidates == ("g_star",)
    assert loaded.configs["turn_threehold"].detect.candidates == ("turn_threehold",)
    assert loaded.configs["hdbscan"].detect.cluster_algorithm == "hdbscan"
    assert len({refine_key(loaded.case, cfg) for cfg in loaded.configs.values()}) == 1
    assert len({cfg.snapshot_parameters()["config_digest"] for cfg in loaded.configs.values()}) == 3
    assert loaded.configs["hdbscan"].effective_snapshot()["semantic_config"]["detect"]["cluster_algorithm"] == "hdbscan"
    assert load_workspace(original.config_path, password="test", validate_run=False).case is None

    counts = setup_stages(monkeypatch)
    records = runner.run_batch(loaded)
    assert counts == {"refine": 1, "detect": 3}
    assert all(record["status"] == "completed" for record in records)
    restored_path = tmp_path / "restored.toml"
    restored_path.write_text(toml_text(records[-1]["draft_snapshot"]))
    restored = load_workspace(restored_path, password="test")
    assert restored.schemes == ("hdbscan",)
    assert restored.configs["hdbscan"].detect == loaded.configs["hdbscan"].detect
