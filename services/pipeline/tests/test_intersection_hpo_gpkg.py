"""Direct GPKG support including curves and optional classification."""
from dataclasses import replace
import json
import shutil
import subprocess

import pytest
from shapely.geometry import box, mapping

from track.v2.experiments.config import load_case
from track.v2.experiments.intersection_hpo.labels import INTERSECTION_TYPES, UNCLASSIFIED, load_labels, load_polygon_file
from track.v2.experiments.intersection_hpo.manifests import audit_data, build_manifests
from test_intersection_hpo_manifests import fixture

pytestmark = pytest.mark.skipif(not shutil.which("ogr2ogr"), reason="GDAL required for real GPKG conversion")


def make_gpkg(tmp_path, name, features):
    source = tmp_path / f"{name}.geojson"
    source.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    target = tmp_path / f"{name}.gpkg"
    subprocess.run(["ogr2ogr", "-f", "GPKG", str(target), str(source), "-nln", name, "-preserve_fid"], check=True, capture_output=True)
    return target


def test_supplied_schema_keeps_attributes_ids_and_unclassified(tmp_path):
    features = [{"type": "Feature", "id": 3, "geometry": mapping(box(110,30,110.01,30.01)),
                 "properties": {"OBJECTID": 15, "CLUSTER_ID": 40, "BUFF_DIST": 20.0}}]
    path = make_gpkg(tmp_path, "labeled_intersections", features)
    labels, metadata = load_labels(path)
    assert labels[0]["id"] == "3"
    assert labels[0]["properties"]["OBJECTID"] == 15
    assert labels[0]["properties"]["type"] == UNCLASSIFIED
    assert metadata["missing_type_count"] == 1
    assert metadata["count"] == 1
    assert metadata["gdal_version"].startswith("GDAL")
    assert metadata["allowed_types"] == [*INTERSECTION_TYPES, UNCLASSIFIED]
    alternate, _ = load_labels(path, id_field="OBJECTID")
    assert alternate[0]["id"] == "15"


def test_curved_multisurface_is_linearized_without_dropping_row(tmp_path):
    source = tmp_path/"curves.csv"
    source.write_text('WKT,OBJECTID\n"MULTISURFACE (CURVEPOLYGON (CIRCULARSTRING (110 30,110.001 30.001,110.002 30,110.001 29.999,110 30)))",1\n')
    target = tmp_path/"curves.gpkg"
    subprocess.run(["ogr2ogr", "-f", "GPKG", str(target), str(source), "-nln", "labeled_intersections", "-a_srs", "EPSG:4326"], check=True, capture_output=True)
    labels, metadata = load_labels(target)
    assert len(labels) == 1
    assert labels[0]["geometry"]["type"] in {"Polygon", "MultiPolygon"}
    assert metadata["curve_linearization"]["arc_step_degrees"] == 1


def test_global_gpkg_labels_use_region_coverage_and_allow_untyped(tmp_path):
    config, workspace, regions = fixture(tmp_path)
    all_labels = []
    for case in workspace.payload["cases"].values():
        truths = json.loads((tmp_path / case["truth_file"]).read_text())["features"]
        for truth in truths:
            truth["id"] = len(all_labels) + 1
            truth["properties"] = {"OBJECTID": len(all_labels)+10}
            all_labels.append(truth)
        case.pop("truth_file")
    labels_path = make_gpkg(tmp_path, "labeled_intersections", all_labels)
    config = replace(config, labels_file=labels_path)
    audit = audit_data(config, workspace)
    assert audit["ready"]
    assert audit["label_catalog"]["missing_type_count"] == 5
    assert build_manifests(config, workspace)["L3"]["label_format"] == "gpkg"
    # Absence of types does not imply absence of manual coverage review.
    regions[0]["properties"]["reviewed_complete"] = False
    config.regions_file.write_text(json.dumps({"type": "FeatureCollection", "features": regions}))
    assert not audit_data(config, workspace)["ready"]


def test_case_reads_selected_gpkg_polygon_without_geojson_conversion(tmp_path):
    features = [{"type": "Feature", "id": 8, "geometry": mapping(box(110,30,110.01,30.01)), "properties": {}}]
    path = make_gpkg(tmp_path, "regions", features)
    case = load_case("roi", {"source_table": "trajectory_points", "region_file": str(path),
                             "region_layer": "regions", "region_id": 8}, tmp_path)
    assert case.geometry["type"] == "Polygon"
    with pytest.raises(ValueError, match="region_id"):
        load_case("roi", {"source_table": "trajectory_points", "region_file": str(path), "region_layer": "regions"}, tmp_path)


def test_negative_gpkg_metadata_and_catalog_diagnostics(tmp_path):
    config, workspace, regions = fixture(tmp_path)
    all_labels = []
    for case in workspace.payload["cases"].values():
        for truth in json.loads((tmp_path / case["truth_file"]).read_text())["features"]:
            truth["id"] = len(all_labels)+1
            truth["properties"] = {}
            all_labels.append(truth)
        case.pop("truth_file")
    labels_path = make_gpkg(tmp_path, "labeled_intersections", all_labels)
    negatives = [feature for feature in regions if feature["properties"]["kind"] == "negative"]
    metadata = {}
    for i, feature in enumerate(negatives):
        metadata[f"negative:{i+1}"] = feature["properties"]
        feature["id"] = i+1
        feature["properties"] = {}
    negative_path = make_gpkg(tmp_path, "negative_regions", negatives)
    for i in range(2):
        case = workspace.payload["cases"][f"case-{i+4}"]
        case.pop("bbox")
        case.update(region_file=str(negative_path), region_layer="negative_regions", region_id=i+1)
    config.regions_file.write_text(json.dumps({"type": "FeatureCollection", "features": [r for r in regions if r not in negatives and not r["id"] in ("roi-4", "roi-5")]}))
    config = replace(config, labels_file=labels_path, negative_regions_file=negative_path, region_metadata=metadata)
    manifests = build_manifests(config, workspace)
    negative = [r for r in manifests["L3"]["regions"] if r["kind"] == "negative"]
    assert len(negative) == 2
    assert all(not r["truths"] for r in negative)
    assert all(r["id"].startswith("negative:") for r in negative)
    assert manifests["L3"]["source_imports"][str(labels_path)]["gdal_version"].startswith("GDAL")
    blocked = audit_data(replace(config, regions_file=tmp_path/"not-yet-supplied.gpkg"), workspace)
    assert not blocked["ready"]
    assert blocked["label_catalog"]["count"] == 5


def test_gpkg_case_run_restores_frozen_geojson_without_layer_fields(tmp_path, monkeypatch):
    from test_experiment_runner import workspace, setup_stages
    from track.v2.experiments import runner
    from track.v2.experiments.config import load_workspace
    from track.v2.cli.experiment import toml_text
    ws = workspace(tmp_path)
    path = make_gpkg(tmp_path, "regions", [{"type": "Feature", "id": 8, "geometry": ws.case.geometry, "properties": {}}])
    payload = ws.payload
    payload["cases"]["case"].pop("bbox")
    payload["cases"]["case"].update(region_file=str(path), region_layer="regions", region_id=8)
    ws.config_path.write_text(toml_text(payload))
    ws = load_workspace(ws.config_path, password="testing-secret")
    setup_stages(monkeypatch)
    records = runner.run_batch(ws)
    case = records[0]["draft_snapshot"]["cases"]["case"]
    assert "region_layer" not in case and "region_id" not in case
    restored = tmp_path/"restored-gpkg-source.toml"
    restored.write_text(toml_text(records[0]["draft_snapshot"]))
    assert load_workspace(restored,password="testing-secret").case.geometry == ws.case.geometry
