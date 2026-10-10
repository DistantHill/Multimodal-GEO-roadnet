"""Region QA and immutable nested selection regression tests."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest
from shapely.geometry import box, mapping

from track.v2.experiments.intersection_hpo.config import HPOConfig, load_hpo_config
from track.v2.experiments.intersection_hpo.manifests import audit_data, build_manifests, write_manifests


def fixture(tmp_path):
    cases, regions = {}, []
    for i in range(7):
        negative = i in (4, 5)
        geometry = mapping(box(110 + i * .1, 30, 110.01 + i * .1, 30.01))
        truth = [] if negative else [{"type": "Feature", "id": f"truth-{i}",
                                     "geometry": mapping(box(110.002+i*.1, 30.002, 110.004+i*.1, 30.004)),
                                     "properties": {"type": "T" if i % 2 else "cross"}}]
        truth_path = tmp_path / f"truth-{i}.json"
        truth_path.write_text(json.dumps({"type": "FeatureCollection", "features": truth}))
        cases[f"case-{i}"] = {"source_table": "trajectory_points", "bbox": [[110+i*.1,30],[110.01+i*.1,30.01]],
                              "truth_file": str(truth_path), "time_start": "2023-01-01T00:00:00Z", "time_end": "2023-01-02T00:00:00Z"}
        regions.append({"type": "Feature", "id": f"roi-{i}", "geometry": geometry,
                        "properties": {"case": f"case-{i}", "spatial_group": f"group-{i}",
                                       "split": "holdout" if i == 6 else "tune",
                                       "kind": "negative" if negative else "positive", "reviewed_complete": True}})
    region_path = tmp_path / "regions.json"
    region_path.write_text(json.dumps({"type": "FeatureCollection", "features": regions}))
    experiment_path = tmp_path / "experiment.toml"
    experiment_path.write_text("# fixture source version")
    config = HPOConfig(tmp_path/"hpo.toml", experiment_path, "threshold", region_path, tmp_path/"out", 42,
                       "labels-v1", "immutable-source-digest", "code-v1", 10, 60, 30, 2, 3, 1, {})
    workspace = SimpleNamespace(payload={"cases": cases, "schemes": {"threshold": {}}},
                                evaluation={"mode": "center_distance", "distance_m": 20},
                                archive_settings={"database": "roadnet_eval"},
                                configs={"threshold": SimpleNamespace(paths=SimpleNamespace(data_root=tmp_path), detect=SimpleNamespace(
                                    candidates=("turn_threshold",), candidate={"turn_threshold": SimpleNamespace(
                                        output_instances=("default",), params={"default": SimpleNamespace(min_turn_deg=30, max_turn_deg=170)})}))})
    return config, workspace, regions


def update_regions(config, regions):
    config.regions_file.write_text(json.dumps({"type": "FeatureCollection", "features": regions}))


def test_nested_deterministic_stratification_and_source_identity(tmp_path):
    config, workspace, regions = fixture(tmp_path)
    manifests = build_manifests(config, workspace)
    assert manifests == build_manifests(config, workspace)
    assert set(manifests["L1"]["region_ids"]) < set(manifests["L2"]["region_ids"]) < set(manifests["L3"]["region_ids"])
    assert {kind for r in manifests["L1"]["regions"] for kind in r["types"]} == {"T", "cross"}
    assert not set(manifests["holdout"]["region_ids"]) & set(manifests["L3"]["region_ids"])
    changed = build_manifests(replace(config, trajectory_snapshot="new-snapshot"), workspace)
    assert changed["L3"]["manifest_hash"] != manifests["L3"]["manifest_hash"]
    # Order in metadata is irrelevant to selection, though original file hash records exact source.
    update_regions(config, list(reversed(regions)))
    assert build_manifests(config, workspace)["L1"]["region_ids"] == manifests["L1"]["region_ids"]


def test_incomplete_coverage_blocks_but_missing_type_remains_unclassified(tmp_path):
    config, workspace, regions = fixture(tmp_path)
    regions[0]["properties"]["reviewed_complete"] = False
    update_regions(config, regions)
    assert not audit_data(config, workspace)["ready"]
    with pytest.raises(ValueError, match="manual label review"):
        build_manifests(config, workspace)
    regions[0]["properties"]["reviewed_complete"] = True
    update_regions(config, regions)
    path = tmp_path/"truth-0.json"
    truth = json.loads(path.read_text())
    truth["features"][0]["properties"] = {}
    path.write_text(json.dumps(truth))
    assert audit_data(config, workspace)["ready"]
    assert "未分类" in build_manifests(config, workspace)["L3"]["regions"][0]["types"]


def test_geometric_holdout_leakage_rejected_even_different_groups(tmp_path):
    config, workspace, regions = fixture(tmp_path)
    regions[-1]["geometry"] = regions[0]["geometry"]
    workspace.payload["cases"]["case-6"]["bbox"] = workspace.payload["cases"]["case-0"]["bbox"]
    workspace.payload["cases"]["case-6"]["truth_file"] = workspace.payload["cases"]["case-0"]["truth_file"]
    update_regions(config, regions)
    with pytest.raises(ValueError, match="spatial leakage"):
        build_manifests(config, workspace)


def test_strict_boundary_rejects_expansion_and_missing_negative_truth(tmp_path):
    config, workspace, regions = fixture(tmp_path)
    regions[0]["geometry"] = mapping(box(109, 29, 112, 32))
    update_regions(config, regions)
    assert "no halo" in audit_data(config, workspace)["blockers"][0]
    regions[0]["geometry"] = mapping(box(110, 30, 110.01, 30.01))
    update_regions(config, regions)
    workspace.payload["cases"]["case-4"].pop("truth_file")
    assert "explicit truth_file" in audit_data(config, workspace)["blockers"][0]


def test_manifest_existing_files_are_verified_and_never_overwritten(tmp_path):
    config, workspace, _ = fixture(tmp_path)
    manifests = build_manifests(config, workspace)
    paths = write_manifests(manifests, config.output_root)
    assert paths == write_manifests(manifests, config.output_root)
    paths["L1"].write_text("corrupted")
    with pytest.raises(ValueError, match="immutable manifest"):
        write_manifests(manifests, config.output_root)
    assert paths["L1"].read_text() == "corrupted"


def test_config_rejects_unknown_halo_and_invalid_search_range(tmp_path):
    text = '''[hpo]
experiment_config="experiment.toml"
scheme="threshold"
regions_file="regions.json"
output_root="out"
label_version="v1"
trajectory_snapshot="sha256:actual"
threshold_min=10
threshold_max=60
baseline=30
l1_positive_count=1
l2_positive_count=2
l2_negative_count=1
'''
    path = tmp_path/"hpo.toml"
    path.write_text(text)
    assert load_hpo_config(path).baseline == 30
    path.write_text(text+'halo_m=20\n')
    with pytest.raises(ValueError, match="unknown"):
        load_hpo_config(path)
    path.write_text(text.replace("baseline=30", "baseline=90"))
    with pytest.raises(ValueError, match="baseline within range"):
        load_hpo_config(path)


def test_centroid_rule_accepts_truth_crossing_region_boundary(tmp_path):
    config, workspace, _ = fixture(tmp_path)
    path = tmp_path/"truth-0.json"
    truth = json.loads(path.read_text())
    truth["features"][0]["geometry"] = mapping(box(109.999, 30.001, 110.003, 30.005))
    path.write_text(json.dumps(truth))
    assert audit_data(config, workspace)["ready"]


def test_baseline_and_search_upper_bound_follow_fixed_scheme(tmp_path):
    config, workspace, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match="baseline must equal"):
        build_manifests(replace(config, baseline=31), workspace)
    with pytest.raises(ValueError, match="fixed max_turn_deg"):
        build_manifests(replace(config, threshold_max=180), workspace)
    workspace.payload["cases"]["case-0"].pop("time_start")
    workspace.payload["cases"]["case-0"].pop("time_end")
    assert "source time bounds" in audit_data(config, workspace)["blockers"][0]


def test_runtime_output_must_stay_under_data_root(tmp_path):
    config, workspace, _ = fixture(tmp_path)
    result = audit_data(replace(config, output_root=tmp_path.parent / "unrelated-output"), workspace)
    assert not result["ready"]
    assert "under configured data_root" in result["blockers"][0]


def test_within_split_shared_boundary_is_rejected(tmp_path):
    config, workspace, regions = fixture(tmp_path)
    # Make two negative regions share an edge; no truth edits are needed.
    regions[5]["geometry"] = mapping(box(110.41, 30, 110.42, 30.01))
    workspace.payload["cases"]["case-5"]["bbox"] = [[110.41,30],[110.42,30.01]]
    update_regions(config, regions)
    assert "overlapping regions within split" in audit_data(config, workspace)["blockers"][0]
