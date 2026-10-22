from __future__ import annotations

import copy
import json
from pathlib import Path
import tomllib

import optuna
import pytest
from shapely.geometry import Point, box, mapping

from pipeline.labels import LabelSet, Region
from pipeline.resources import ResourceLimitExceeded
from pipeline.tune import RegionMedianPruner, tune, validate_search_space


def feature(identifier, x):
    return {"type": "Feature", "id": str(identifier), "geometry": mapping(Point(x, 5)), "properties": {}}


def settings(tmp_path, trials=12):
    return {"input": {"processing_crs": "EPSG:32650"}, "output": {"root": str(tmp_path)},
            "refine": {}, "detect": {"quality": 0}, "clip": {}, "fit": {}, "network": {},
            "database": {"enabled": False}, "labels": {},
            "tuning": {"study_name": "smoke", "seed": 42, "n_trials": trials,
                       "startup_trials": 1, "warmup_steps": 0, "objective": "intersection_f1",
                       "matching_radius_m": 2.0,
                       "search_space": {"detect.quality": {"type": "int", "low": 0, "high": 1}}}}


def labels():
    regions = [Region(str(i), box(i * 20, 0, i * 20 + 10, 10), reviewed_complete=True) for i in range(3)]
    regions.append(Region("holdout", box(60, 0, 70, 10), split="holdout", reviewed_complete=True))
    return LabelSet("EPSG:32650", [feature(str(i), i * 20 + 5) for i in range(4)], [], regions,
                    {"layers": {"intersections": {}}, "warnings": []}, "labels-identity")


@pytest.fixture(autouse=True)
def stable_code_identity(monkeypatch):
    # Other workbench modules can be edited by concurrent development workers.
    # This fixture isolates resume behavior from unrelated source changes.
    import importlib
    module = importlib.import_module("pipeline.tune")
    monkeypatch.setattr(module, "_code_identity", lambda: {"version": "test"})


def test_actual_optuna_pruning_skips_remaining_regions_and_excludes_holdout(tmp_path):
    config = settings(tmp_path)
    config_path = tmp_path / "pipeline.toml"
    config_path.write_text("# original untouched\n")
    calls = []

    def runner(effective, region, output_dir):
        calls.append((region.id, effective["detect"]["quality"], str(output_dir)))
        assert effective["database"]["enabled"] is False
        x = (region.bounds[0] + region.bounds[2]) / 2
        # The first seeded trial chooses 0; 1 is a deliberately inferior setting.
        return {"intersections": [feature("p", x)] if effective["detect"]["quality"] == 0 else [],
                "centerlines": [], "run_id": output_dir.name}

    result = tune(config, config_path, runner, {"source_sha256": "abc"}, labels=labels())
    assert result["trial_counts"]["PRUNED"] > 0
    assert result["trial_counts"]["COMPLETE"] > 0
    assert result["best_value"] == 1
    assert result["best_params"] == {"detect.quality": 0}
    assert result["holdout"]["value"] == 1
    assert result["holdout"]["used_for_selection"] is False
    assert [region for region, _, path in calls if "/trials/" in path].count("holdout") == 0
    assert sum(region == "holdout" for region, _, _ in calls) == 1
    assert len(calls) < config["tuning"]["n_trials"] * 3 + 1
    trials = json.loads((tmp_path / "studies/smoke/trials.json").read_text())
    for trial in trials:
        if trial["state"] == "PRUNED":
            assert len(trial["intermediate_values"]) < 3
    assert config_path.read_text() == "# original untouched\n"
    exported = tomllib.loads(Path(result["best_config_path"]).read_text())
    assert exported["detect"]["quality"] == 0


def test_resume_uses_total_budget_and_rejects_changed_identity(tmp_path):
    config = settings(tmp_path, trials=1)
    config_path = tmp_path / "pipeline.toml"
    config_path.write_text("# config\n")
    calls = []

    def runner(effective, region, output_dir):
        calls.append(region.id)
        return {"intersections": [feature("p", region.bounds[0] + 5)]}

    first = tune(config, config_path, runner, {"source_sha256": "abc"}, labels=labels())
    assert len(calls) == 4
    again = tune(config, config_path, runner, {"source_sha256": "abc"}, labels=labels())
    assert len(calls) == 4 and again["best_value"] == first["best_value"]
    config["tuning"]["n_trials"] = 2
    extended = tune(config, config_path, runner, {"source_sha256": "abc"}, labels=labels())
    assert extended["trial_counts"]["COMPLETE"] == 2
    assert len(calls) == 7
    with pytest.raises(ValueError, match="identity changed"):
        tune(config, config_path, runner, {"source_sha256": "changed"}, labels=labels())


def test_failure_is_recorded_and_stops_instead_of_returning_zero(tmp_path):
    config = settings(tmp_path, trials=2)
    source = tmp_path / "pipeline.toml"
    source.write_text("")

    def runner(*args):
        raise OSError("source became unavailable")

    with pytest.raises(OSError, match="unavailable"):
        tune(config, source, runner, {}, labels=labels())
    trials = json.loads((tmp_path / "studies/smoke/trials.json").read_text())
    assert len(trials) == 1 and trials[0]["state"] == "FAIL" and trials[0]["value"] is None


def test_irrelevant_downstream_parameters_are_rejected(tmp_path):
    config = settings(tmp_path)
    config["fit"]["smoothing"] = 0.5
    config["tuning"]["search_space"] = {"fit.smoothing": {"type": "float", "low": 0.0, "high": 1.0}}
    with pytest.raises(ValueError, match="cannot affect"):
        validate_search_space(config)


@pytest.mark.parametrize("name", ["clip.include_boundary_clips", "fit.emit_directional"])
def test_diagnostic_export_parameters_are_not_tuning_dimensions(tmp_path, name):
    config = settings(tmp_path)
    section, field = name.split(".")
    config[section][field] = True
    config["tuning"]["objective"] = "centerline_f1"
    config["tuning"]["search_space"] = {name: {"type": "categorical", "choices": [True, False]}}
    with pytest.raises(ValueError, match="diagnostic exports"):
        validate_search_space(config)


def test_single_region_and_no_holdout_are_explicit(tmp_path):
    config = settings(tmp_path, trials=1)
    current = labels()
    current.regions = current.regions[:1]
    current.metadata["warnings"] = ["No holdout region configured"]
    result = tune(config, tmp_path / "pipeline.toml",
                  lambda effective, region, path: {"intersections": [feature("p", 5)]},
                  {}, labels=current)
    assert result["holdout"]["status"] == "UNAVAILABLE"
    assert any("no intermediate pruning" in warning for warning in result["warnings"])


def test_resource_budget_prunes_instead_of_scoring_zero(tmp_path):
    config = settings(tmp_path, trials=2)
    first = True

    def runner(effective, region, output_dir):
        nonlocal first
        if first:
            first = False
            raise ResourceLimitExceeded("sampled process tree RSS exceeded soft memory budget")
        return {"intersections": [feature("p", region.bounds[0] + 5)]}

    result = tune(config, tmp_path / "pipeline.toml", runner, {}, labels=labels())
    assert result["trial_counts"]["PRUNED"] == 1
    records = json.loads((tmp_path / "studies/smoke/trials.json").read_text())
    assert records[0]["value"] is None
    assert records[0]["user_attrs"]["reason_kind"] == "resource_budget"
    assert "safe boundary" in records[0]["user_attrs"]["resource_budget_semantics"]


def test_holdout_retry_preserves_failed_attempt(tmp_path):
    config = settings(tmp_path, trials=1)
    fail = True
    attempted = []

    def runner(effective, region, output_dir):
        if region.split == "holdout":
            output_dir.mkdir(parents=True)
            (output_dir / "marker").write_text("preserved")
            attempted.append(output_dir)
            if fail:
                raise OSError("temporary holdout input failure")
        return {"intersections": [feature("p", region.bounds[0] + 5)]}

    with pytest.raises(OSError, match="temporary"):
        tune(config, tmp_path / "pipeline.toml", runner, {}, labels=labels())
    fail = False
    result = tune(config, tmp_path / "pipeline.toml", runner, {}, labels=labels())
    assert result["trial_counts"]["COMPLETE"] == 1
    assert attempted[0] != attempted[1]
    assert (attempted[0] / "marker").read_text() == "preserved"


def test_pruner_compares_same_prefix_not_earlier_easier_prefix():
    study = optuna.create_study(direction="maximize", pruner=RegionMedianPruner(1, 0))
    baseline = study.ask()
    baseline.report(0.8, 0)
    baseline.report(0.8, 1)
    study.tell(baseline, 0.8)
    candidate = study.ask()
    candidate.report(1.0, 0)
    assert not candidate.should_prune()
    candidate.report(0.2, 1)
    assert candidate.should_prune()
