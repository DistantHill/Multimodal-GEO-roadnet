from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
from pyogrio.raw import write
from pyproj import Transformer
import pytest
from shapely.geometry import LineString, Point, box, mapping, shape

from pipeline.evaluation import aggregate_scores, match_intersections, score_centerlines, score_region
from pipeline.labels import LabelSet, Region, list_layers, load_labels, prepare_labels, vector_fingerprint


def feature(identifier, geometry, **properties):
    return {"type": "Feature", "id": str(identifier), "geometry": mapping(geometry),
            "properties": properties}


def write_layer(path: Path, geometries, fields, *, layer=None, crs="EPSG:32650"):
    write(path, np.array([geometry.wkb for geometry in geometries], dtype=object),
          [np.array(values) for values in fields.values()], list(fields), layer=layer,
          crs=crs, geometry_type=geometries[0].geom_type)


def test_global_one_to_one_matching_prevents_greedy_false_negatives():
    actual = [feature("a", Point(0, 0)), feature("b", Point(10, 0))]
    predicted = [feature("p1", Point(4, 0)), feature("p2", Point(-4, 0))]
    score = match_intersections(predicted, actual, radius_m=7)
    assert (score["tp"], score["fp"], score["fn"]) == (2, 0, 0)
    assert {tuple((item["prediction_id"], item["truth_id"])) for item in score["matches"]} == {
        ("p1", "b"), ("p2", "a")}


def test_duplicate_detections_are_false_positives():
    score = match_intersections([feature("p1", Point(0, 0)), feature("p2", Point(1, 0))],
                                [feature("a", Point(0, 0), type="T")], 5)
    assert (score["tp"], score["fp"], score["fn"]) == (1, 1, 0)
    assert score["f1"] == pytest.approx(2 / 3)
    assert score["per_type_recall"]["T"]["recall"] == 1


def test_unreviewed_regions_reject_false_positive_metrics():
    region = Region("one", box(0, 0, 10, 10))
    labels = LabelSet("EPSG:32650", [], [], [region], {"layers": {"intersections": {}}}, "x")
    with pytest.raises(ValueError, match="not fully reviewed"):
        score_region({}, labels, region, {})


def test_touching_regions_have_unique_boundary_ownership():
    regions = [Region("a", box(0, 0, 10, 10), reviewed_complete=True),
               Region("b", box(10, 0, 20, 10), reviewed_complete=True)]
    actual = [feature("edge", Point(10, 5))]
    labels = LabelSet("EPSG:32650", actual, [], regions, {"layers": {"intersections": {}}}, "x")
    scores = [score_region({"intersections": actual}, labels, region, {}) for region in regions]
    result = aggregate_scores(scores)["intersections"]
    assert (result["tp"], result["fp"], result["fn"]) == (1, 0, 0)


def test_line_coverage_is_length_weighted_and_excludes_unreviewed_space():
    region = Region("a", box(0, 0, 100, 20), reviewed_complete=True)
    actual = [feature("truth", LineString([(0, 10), (100, 10)]))]
    predicted = [feature("half", LineString([(0, 10), (50, 10)])),
                 feature("outside", LineString([(200, 10), (300, 10)]))]
    score = score_centerlines(predicted, actual, region, tolerance_m=1)
    assert score["precision"] == pytest.approx(1.0)
    assert score["recall"] == pytest.approx(0.51, abs=0.001)
    assert score["predicted_length_m"] == 50
    assert score["metric_kind"] == "length_weighted_geometric_coverage"


def test_directional_diagnostics_do_not_change_physical_road_score():
    region = Region("a", box(0, 0, 100, 100), reviewed_complete=True)
    actual = [feature("truth", LineString([(0, 10), (100, 10)]))]
    physical = feature("combined", LineString([(0, 10), (100, 10)]), variant="combined")
    diagnostic = feature("directional", LineString([(0, 90), (100, 90)]), variant="a_to_b")
    score = score_centerlines([physical, diagnostic], actual, region, tolerance_m=1)
    assert score["predicted_length_m"] == 100
    assert score["f1"] == 1


def test_gpkg_import_transforms_crs_and_exports_independent_snapshots(tmp_path):
    path = tmp_path / "labels.gpkg"
    geographic = Point(117, 39)
    write_layer(path, [geographic], {"key": ["intersection-1"], "type": ["T"], "note": ["保留属性"]},
                layer="truth", crs="EPSG:4326")
    x, y = Transformer.from_crs(4326, 32650, always_xy=True).transform(117, 39)
    write_layer(path, [box(x - 100, y - 100, x + 100, y + 100)],
                {"key": ["r"], "split": ["tune"], "reviewed": [1]}, layer="regions")
    labels = load_labels({"intersections": {"path": str(path), "layer": "truth", "id_field": "key"},
                          "regions": {"path": str(path), "layer": "regions", "id_field": "key"}}, 32650)
    assert shape(labels.intersections[0]["geometry"]).distance(Point(x, y)) < 1e-6
    assert labels.intersections[0]["properties"]["note"] == "保留属性"
    assert labels.regions[0].reviewed_complete
    assert {row["layer"] for row in list_layers(path)} == {"truth", "regions"}
    output = prepare_labels(labels, tmp_path / "preview")
    assert Path(output["preview"]).read_text().startswith("<svg")
    table = pq.read_table(output["outputs"]["intersections"]["parquet"])
    assert "geometry" in table.column_names and b"geo" in table.schema.metadata
    assert json.loads(table["properties_json"][0].as_py())["note"] == "保留属性"
    exported = json.loads(Path(output["outputs"]["intersections"]["geojson"]).read_text())
    assert shape(exported["features"][0]["geometry"]).distance(geographic) < 1e-9


def test_shapefile_attributes_and_sidecar_changes_affect_identity(tmp_path):
    path = tmp_path / "truth.shp"
    write_layer(path, [Point(5, 5)], {"key": ["a"], "name": ["before"]})
    settings = {"intersections": {"path": str(path), "id_field": "key"},
                "reviewed_bbox": [0, 0, 10, 10], "reviewed_complete": True}
    before = load_labels(settings, 32650)
    fingerprint = vector_fingerprint(path)
    write_layer(path, [Point(5, 5)], {"key": ["a"], "name": ["after"]})
    after = load_labels(settings, 32650)
    assert before.identity != after.identity
    assert vector_fingerprint(path) != fingerprint
    assert after.intersections[0]["properties"]["name"] == "after"


def test_missing_crs_and_duplicate_ids_are_rejected(tmp_path):
    path = tmp_path / "truth.shp"
    write_layer(path, [Point(5, 5)], {"key": ["a"]})
    path.with_suffix(".prj").unlink()
    with pytest.raises(ValueError, match="no CRS"):
        load_labels({"intersections": {"path": str(path), "id_field": "key"}}, 32650)
    duplicate = tmp_path / "duplicate.gpkg"
    write_layer(duplicate, [Point(5, 5), Point(6, 6)], {"key": ["a", "a"]})
    with pytest.raises(ValueError, match="unique"):
        load_labels({"intersections": {"path": str(duplicate), "id_field": "key"}}, 32650)


def test_geographic_processing_crs_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="projected CRS"):
        load_labels({}, 4326)


def test_region_overlap_is_rejected(tmp_path):
    path = tmp_path / "labels.gpkg"
    write_layer(path, [Point(5, 5)], {"key": ["a"]}, layer="truth")
    write_layer(path, [box(0, 0, 10, 10), box(5, 5, 15, 15)],
                {"key": ["one", "two"], "reviewed": [1, 1]}, layer="regions")
    with pytest.raises(ValueError, match="overlap"):
        load_labels({"intersections": {"path": str(path), "layer": "truth", "id_field": "key"},
                     "regions": {"path": str(path), "layer": "regions", "id_field": "key"}}, 32650)


def test_unknown_label_settings_and_ambiguous_regions_are_rejected():
    with pytest.raises(ValueError, match="unknown labels keys"):
        load_labels({"intersetions": {}}, 32650)
    with pytest.raises(ValueError, match="unknown labels.intersections"):
        load_labels({"intersections": {"paht": "truth.shp"}}, 32650)
    with pytest.raises(ValueError, match="not both"):
        load_labels({"regions": {"path": "regions.gpkg"}, "reviewed_bbox": [0, 0, 10, 10]}, 32650)


def test_negative_region_false_positives_do_not_create_true_matches():
    result = match_intersections([feature("false", Point(0, 0))], [], 10)
    assert (result["tp"], result["fp"], result["fn"], result["f1"]) == (0, 1, 0, 0)


def test_centerline_on_shared_boundary_is_counted_once():
    regions = [Region("a", box(0, 0, 10, 10), reviewed_complete=True),
               Region("b", box(10, 0, 20, 10), reviewed_complete=True)]
    lines = [feature("edge", LineString([(10, 1), (10, 9)]))]
    labels = LabelSet("EPSG:32650", [], lines, regions, {"layers": {"centerlines": {}}}, "x")
    scores = [score_region({"centerlines": lines}, labels, region, {}) for region in regions]
    aggregate = aggregate_scores(scores)["centerlines"]
    assert aggregate["predicted_length_m"] == 8
    assert aggregate["truth_length_m"] == 8
