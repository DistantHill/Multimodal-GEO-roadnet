"""Fixed local-input sampling, duplicate provenance and polygon ROI semantics."""
import hashlib
import json

import pytest
from pyproj import Transformer
from shapely import transform
from shapely.geometry import Polygon, box, mapping, shape
from shapely.ops import unary_union

from track.v2.experiments.intersection_hpo.local_inputs import prepare_local_inputs


def write_labels(tmp_path, features):
    path = tmp_path / "labels.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return path


def feature(identifier, geometry, label_type=None):
    return {"type": "Feature", "id": identifier, "geometry": mapping(geometry),
            "properties": {"type": label_type, "original": identifier}}


def catalog():
    types = ["十字路口", "T字路口", None, "future type"]
    return [feature(str(i), box(109.1 + (i % 11) * .012, 34.7 + (i // 11) * .012,
                               109.1 + (i % 11) * .012 + .0002 * (1 + i % 3),
                               34.7 + (i // 11) * .012 + .0002), types[i % 4])
            for i in range(110)]


def test_duplicate_topology_is_removed_but_nearby_geometry_is_retained(tmp_path, caplog):
    geometry = box(109.2, 34.8, 109.201, 34.801)
    reversed_ring = Polygon(list(geometry.exterior.coords)[::-1])
    near = box(109.2 + 1e-9, 34.8, 109.201, 34.801)
    path = write_labels(tmp_path, [feature("305", geometry), feature("306", reversed_ring), feature("307", near)])
    source_before = path.read_bytes()
    result = prepare_local_inputs(path, tmp_path / "out", sizes=(1, 2))
    assert result["effective_label_count"] == 2
    assert result["duplicate_groups"] == [{"representative_id": "305", "source_ids": ["305", "306"]}]
    assert result["source_id_map"] == {"305": "305", "306": "305", "307": "307"}
    assert "Exact duplicate" in caplog.text
    assert path.read_bytes() == source_before
    assert result["source"]["sha256"] == hashlib.sha256(source_before).hexdigest()


def test_fixed_seed_nested_twenty_fifty_all_and_persisted_hash(tmp_path):
    features = catalog()
    features.append(feature("duplicate", shape(features[0]["geometry"])))
    path = write_labels(tmp_path, features)
    first = prepare_local_inputs(path, tmp_path / "first")
    second = prepare_local_inputs(path, tmp_path / "second")
    assert first == second
    levels = first["levels"]
    assert [len(levels[name]["label_ids"]) for name in ("L1", "L2", "L3")] == [20, 50, 110]
    assert levels["L2"]["label_ids"][:20] == levels["L1"]["label_ids"]
    assert levels["L3"]["label_ids"][:50] == levels["L2"]["label_ids"]
    persisted = json.loads((tmp_path / "first" / "local_inputs_manifest.json").read_text())
    assert persisted["sha256"] == first["sha256"]
    unsigned = {key: value for key, value in persisted.items() if key != "sha256"}
    encoded = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert hashlib.sha256(encoded.encode()).hexdigest() == persisted["sha256"]
    assert first["levels"]["L3"]["effective_count"] == 110


def test_sampling_covers_observed_types_space_and_geometry_not_file_head(tmp_path):
    path = write_labels(tmp_path, catalog())
    result = prepare_local_inputs(path, tmp_path / "out")
    records = result["stratification"]["features"]
    selected = [records[key] for key in result["levels"]["L1"]["label_ids"]]
    assert {row["type"] for row in selected} == {"十字路口", "T字路口", "未分类", "future type"}
    assert {row["space"] for row in selected} == {row["space"] for row in records.values()}
    assert {row["structure"] for row in selected} == {row["structure"] for row in records.values()}
    assert set(result["levels"]["L1"]["label_ids"]) != {str(i) for i in range(20)}
    for axis, strata in result["levels"]["L1"]["quotas"].items():
        assert sum(row["target"] for row in strata.values()) == 20
        assert sum(row["actual"] for row in strata.values()) == 20
        assert all(row["target"] <= row["population"] for row in strata.values())


def test_buffer_is_200_projected_metres_of_each_original_face(tmp_path):
    shapes = [box(109.2, 34.8, 109.2002, 34.8002), box(109.22, 34.8, 109.2202, 34.8002)]
    path = write_labels(tmp_path, [feature(str(i), geometry) for i, geometry in enumerate(shapes)])
    result = prepare_local_inputs(path, tmp_path / "out", sizes=(1, 2))
    level = result["levels"]["L3"]
    assert shape(level["evaluation_region"]).equals(unary_union(shapes))
    forward = Transformer.from_crs(4326, 32649, always_xy=True).transform
    expected = unary_union([transform(geometry, forward, interleaved=False).buffer(200) for geometry in shapes])
    actual = transform(shape(level["computation_region"]), forward, interleaved=False)
    assert actual.hausdorff_distance(expected) < .000001
    assert len(level["truth"]["features"]) == 2
    for truth in level["truth"]["features"]:
        assert truth["geometry"] == json.loads(json.dumps(mapping(shapes[int(truth["id"])])))


def test_unselected_truth_centroid_in_roi_is_included_without_cutting(tmp_path):
    # One face surrounds every other centroid. Give it the only known class so
    # the deterministic marginal coverage priority selects it at size one.
    outer = box(109.2, 34.8, 109.21, 34.81)
    inner = box(109.207, 34.804, 109.211, 34.806)
    path = write_labels(tmp_path, [feature("outer-0", outer, "known"), feature("inner", inner)])
    result = prepare_local_inputs(path, tmp_path / "out", sizes=(1, 2))
    level = result["levels"]["L1"]
    assert level["label_ids"] == ["outer-0"]
    assert level["selected_count"] == 1
    assert level["effective_count"] == 2
    assert level["additional_truth_ids"] == ["inner"]
    assert shape(next(f["geometry"] for f in level["truth"]["features"] if f["id"] == "inner")).equals(inner)
    assert not outer.covers(inner)


def test_sparse_cross_strata_preserve_nested_members_and_order_independence(tmp_path):
    features = catalog()[:14]
    path = write_labels(tmp_path, features)
    first = prepare_local_inputs(path, tmp_path / "first", sizes=(4, 9))
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features[::-1]}))
    second = prepare_local_inputs(path, tmp_path / "second", sizes=(4, 9))
    assert first["levels"] == second["levels"]
    assert len(first["levels"]["L1"]["quotas"]["composite"]) > 4
    assert len({first["stratification"]["features"][key]["type"]
                for key in first["levels"]["L1"]["label_ids"]}) == 4
    assert first["levels"]["L2"]["label_ids"][:4] == first["levels"]["L1"]["label_ids"]


def test_invalid_sizes_and_nonmetric_crs_fail_before_writing(tmp_path):
    path = write_labels(tmp_path, catalog())
    for sizes in ((0, 50), (50, 20), (1,), (True, 50)):
        with pytest.raises(ValueError, match="sizes"):
            prepare_local_inputs(path, tmp_path / "out", sizes=sizes)
    with pytest.raises(ValueError, match="metre"):
        prepare_local_inputs(path, tmp_path / "out", processing_srid=4326)
    assert not (tmp_path / "out").exists()
