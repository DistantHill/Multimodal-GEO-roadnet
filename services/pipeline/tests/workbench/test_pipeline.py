from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
import numpy as np
from scipy.optimize import linear_sum_assignment
from shapely.geometry import shape
import tomlkit

from fixtures import make_config, make_dataset
from pipeline.evaluation import aggregate_scores, score_region
from pipeline.labels import load_labels
from pipeline.source import prepare_source


def _source_path(root, config):
    path = root / "pipeline.toml"
    path.write_text(tomlkit.dumps(config), encoding="utf-8")
    return path


def _rows(root, path):
    return pq.read_table(root / path).to_pylist()


def test_real_pipeline_runs_without_database_and_keeps_independent_artifacts(tmp_path):
    from pipeline.runner import run_pipeline

    dataset = make_dataset(tmp_path / "source")
    config = make_config(dataset, tmp_path / "output")
    config_path = _source_path(tmp_path, config)
    source = prepare_source(config, tmp_path / "prepared")
    assert source.rows == dataset["row_count"] and source.rejected_rows == 0
    # PreparedSource is the whole input: no later stage may query the original.
    dataset["parquet"].unlink()
    result = run_pipeline(config, config_path, source=source)
    root = Path(result["run_dir"])
    assert json.loads((root / "run.json").read_text())["status"] == "COMPLETE"
    assert result["metrics"]["status"] == "COMPLETE"
    assert result["metrics"]["overall"]["wall_s"] > 0
    assert len(result["intersections"]) == 3
    assert len(result["centerlines"]) >= 2
    assert result["network"]["edges"]
    for stage in ("refine", "detect", "clip", "fit", "network"):
        manifest = json.loads((root / stage / "manifest.json").read_text())
        assert manifest["status"] == "COMPLETE"
        for artifact in manifest["datasets"].values():
            path = root / stage / artifact["path"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
            assert pq.ParquetFile(path).metadata.num_rows == artifact["rows"]
    points = _rows(root, "refine/points.parquet")
    assert points and all(point["geometry"] and point["x"] and point["y"] for point in points)
    original = [point for point in points if point["source_id"] is not None]
    assert original
    assert all("fleet" in json.loads(point["attributes_json"]) for point in original)
    point_ids = {point["point_id"] for point in points}
    assert len(point_ids) == len(points)
    segments = _rows(root, "refine/segments.parquet")
    assert all(row["from_point_id"] in point_ids and row["to_point_id"] in point_ids for row in segments)
    segment_ids = {row["segment_id"] for row in segments}
    events = _rows(root, "detect/candidates.parquet")
    assert all(row["segment_a_id"] is None or row["segment_a_id"] in segment_ids for row in events)
    assert all(row["segment_b_id"] is None or row["segment_b_id"] in segment_ids for row in events)
    clips = _rows(root, "clip/clips.parquet")
    clip_ids = {row["clip_id"] for row in clips}
    assert clips and all(row["geometry"] and row["point_attributes_json"] for row in clips)
    centerline_ids = {str(feature["id"]) for feature in result["centerlines"]}
    membership = _rows(root, "fit/memberships.parquet")
    assert membership
    assert all(row["centerline_id"] in centerline_ids and row["clip_id"] in clip_ids for row in membership)
    labels = load_labels(config["labels"], config["input"]["processing_crs"])
    metrics = aggregate_scores([score_region(result, labels, region, config["tuning"])
                                for region in labels.regions])
    assert metrics["intersections"]["f1"] == 1.0


def test_csv_and_parquet_produce_the_same_road_geometry(tmp_path):
    from pipeline.runner import run_pipeline

    dataset = make_dataset(tmp_path / "source", replicas=3)
    results = []
    for kind in ("csv", "parquet"):
        config = make_config(dataset, tmp_path / kind, kind=kind)
        config_path = _source_path(tmp_path, config)
        result = run_pipeline(config, config_path)
        results.append(result)
    for layer in ("intersections", "centerlines"):
        # Orientation/reduction order can change collinear sample locations and
        # introduce ~1 ULP at large XY. Compare geometry, not vertex encodings.
        geometries = [[shape(feature["geometry"]) for feature in result[layer]] for result in results]
        assert len(geometries[0]) == len(geometries[1]) > 0
        costs = np.asarray([[a.hausdorff_distance(b) for b in geometries[1]] for a in geometries[0]])
        left, right = linear_sum_assignment(costs)
        assert max(costs[left, right]) < 1e-6
        assert all(abs(geometries[0][i].length - geometries[1][j].length) < 1e-6
                   for i, j in zip(left, right))


def test_bad_source_primary_keys_fail_before_running_stages(tmp_path):
    import pyarrow as pa

    dataset = make_dataset(tmp_path / "source", replicas=2)
    config = make_config(dataset, tmp_path / "output")
    data = pq.read_table(dataset["parquet"])
    pq.write_table(pa.concat_tables([data, data.slice(0, 1)]), dataset["parquet"])
    with pytest.raises(ValueError, match="Duplicate source primary key"):
        prepare_source(config, tmp_path / "prepared")
