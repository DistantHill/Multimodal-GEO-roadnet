"""Local caches contain exact inside runs and retain source identities."""
from types import SimpleNamespace
from uuid import uuid4

import duckdb
import polars as pl
import pytest
from shapely.geometry import Polygon, box, mapping

from track.v2.experiments.intersection_hpo import local_refine
from track.v2.experiments.intersection_hpo.local_inputs import _hash
from track.v2.artifacts import publish_dataset
from track.v2.artifacts.parquet_manifest import build_descriptor
from v2.config_factory import intersect_config, runtime_config
from v2.test_refine_parquet_reader import _published, _published_tracks


def inputs(region):
    value = {"version": "fixture", "levels": {name: {"computation_region": mapping(region)}
                                              for name in ("L1", "L2", "L3")}}
    value["sha256"] = _hash(value)
    return value


def cache(tmp_path, tracks, region, **kwargs):
    parent = tmp_path / "parent"
    parent.mkdir()
    descriptor = _published_tracks(parent, tracks)
    runtime = runtime_config(duckdb_threads=1)
    manifest = inputs(region)
    result = local_refine.prepare_local_refine(parent, descriptor, "present", runtime,
        manifest, tmp_path / "cache", batch_size=2, **kwargs)
    return result, runtime, parent, descriptor, manifest


def test_exact_roi_time_boundary_and_leave_reenter_split_without_neighbours(tmp_path):
    source = [(i, x, 1, 90) for i, x in enumerate((0, 1, 20, 2, 3, 20, 1))]
    result, runtime, *_ = cache(tmp_path, [("v", 8, source)], box(120, 30, 120.000003, 30.000003),
        time_start="1970-01-01T00:00:00+00:00", time_end="1970-01-01T00:00:06+00:00")
    for level in result["levels"].values():
        assert (level["point_count"], level["fragment_count"]) == (4, 2)
        with local_refine.LocalRefineReader(level, runtime) as reader:
            tracks = list(reader.iter_tracks("public.trajectory_points", batch_size=1))
            assert [len(track) for track in tracks] == [2, 2]
            assert [track.subtrack_index for track in tracks] == [0, 1]
            assert [track.timestamp.tolist() for track in tracks] == [[0, 1], [3, 4]]
            turns = list(reader.iter_turn_candidates("public.trajectory_points", intersect_config(turn_window=1)))
            assert turns == []
            selections = list(reader.iter_topology_tracks("public.trajectory_points",
                [SimpleNamespace(x_min=-1, y_min=-1, x_max=30, y_max=30)], batch_size=1))
            assert [len(selection.points) for selection in selections] == [2, 2]
        frame = pl.read_parquet(str(level["publication_root"]) + "/" + level["descriptor"].relative_path + "/kept_points/points.parquet")
        assert frame["original_subtrack_index"].to_list() == [8] * 4
        assert frame["original_refined_sequence_in_subtrack"].to_list() == [0, 1, 3, 4]
        assert frame["refined_sequence_in_subtrack"].to_list() == [0, 1, 0, 1]


def test_roi_hole_is_excluded_before_fragments_are_numbered(tmp_path):
    outer = box(119.999999, 29.999999, 120.000011, 30.000011)
    hole = box(120.000004, 30.000004, 120.000006, 30.000006)
    polygon = Polygon(outer.exterior.coords, [hole.exterior.coords])
    result, runtime, *_ = cache(tmp_path, [("v", 4, [(i, x, 5, 0) for i, x in enumerate((0, 5, 1))])], polygon)
    assert result["levels"]["L1"]["point_count"] == 2
    with local_refine.LocalRefineReader(result["levels"]["L1"], runtime) as reader:
        assert [track.timestamp.tolist() for track in reader.iter_tracks("public.trajectory_points")] == [[0], [2]]


def test_cache_reuse_does_not_read_or_validate_parent_again(tmp_path, monkeypatch):
    result, runtime, parent, descriptor, manifest = cache(tmp_path,
        [("v", 1, [(0, 0, 0, 90), (1, 1, 1, 90)])], box(119.99, 29.99, 120.01, 30.01))
    def forbidden(*args, **kwargs):
        raise AssertionError("parent schema was inspected while reusing local cache")
    monkeypatch.setattr(local_refine, "_validate_schema", forbidden)
    reused = local_refine.prepare_local_refine(parent, descriptor, "present", runtime,
        manifest, tmp_path / "cache")
    assert reused == result
    with local_refine.LocalRefineReader(reused["levels"]["L1"], runtime) as reader:
        def validated_guard(connection):
            if not reader._source_validated:
                forbidden()
        monkeypatch.setattr(reader, "_validate_source", validated_guard)
        assert reader._source_validated
        assert reader.track_count() == 1
        assert len(list(reader.iter_tracks("public.trajectory_points"))) == 1


def test_kept_and_injected_points_share_one_local_contiguous_sequence(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    descriptor = _published(parent)
    runtime = runtime_config(duckdb_threads=1)
    result = local_refine.prepare_local_refine(parent, descriptor, "present", runtime,
        inputs(box(119.9, 29.9, 120.1, 30.1)), tmp_path / "cache", batch_size=1)
    with local_refine.LocalRefineReader(result["levels"]["L1"], runtime) as reader:
        rows = [row for batch in reader.iter_subtrack_batches("v1", 0, batch_size=1) for row in batch]
    assert [(row[4], row[12]) for row in rows] == [(0, "kept"), (1, "injected"), (2, "kept")]


def test_empty_level_is_a_readable_cache_and_corrupt_hash_is_rejected(tmp_path):
    result, runtime, *_ = cache(tmp_path, [("v", 1, [(0, 0, 0, 90)])], box(110, 20, 111, 21))
    level = result["levels"]["L1"]
    assert (level["point_count"], level["fragment_count"]) == (0, 0)
    with local_refine.LocalRefineReader(level, runtime) as reader:
        assert list(reader.iter_tracks("public.trajectory_points")) == []
    bad = inputs(box(119.9, 29.9, 120.1, 30.1))
    bad["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash"):
        local_refine.prepare_local_refine("ignored", level["descriptor"], "present", runtime, bad, tmp_path / "bad")


def test_reuse_verifies_cached_parquet_content_hash(tmp_path):
    result, runtime, parent, descriptor, manifest = cache(tmp_path,
        [("v", 1, [(0, 0, 0, 90), (1, 1, 1, 90)])], box(119.99, 29.99, 120.01, 30.01))
    from pathlib import Path
    level = result["levels"]["L1"]
    path = Path(level["publication_root"]) / descriptor.relative_path / "kept_points" / "points.parquet"
    data = bytearray(path.read_bytes())
    data[10] ^= 1
    path.chmod(0o644)
    path.write_bytes(data)
    path.chmod(0o444)
    with pytest.raises(Exception, match="digest|hash|SHA"):
        local_refine.prepare_local_refine(parent, descriptor, "present", runtime,
            manifest, tmp_path / "cache")


@pytest.mark.parametrize("original_index", [-1, None])
def test_invalid_original_subtrack_identity_is_not_repaired_by_local_numbering(tmp_path, original_index):
    with pytest.raises(local_refine.RefineParquetError, match="original identity"):
        cache(tmp_path, [("v", original_index, [(0, 0, 0, 90)])], box(119.9, 29.9, 120.1, 30.1))


def test_original_subtrack_shared_by_two_vehicles_is_rejected(tmp_path):
    with pytest.raises(local_refine.RefineParquetError, match="original identity"):
        cache(tmp_path, [("first", 1, [(0, 0, 0, 90)]), ("second", 1, [(0, 0, 0, 90)])],
              box(119.9, 29.9, 120.1, 30.1))


@pytest.mark.parametrize("original_sequence", [-1, None])
def test_invalid_original_sequence_is_rejected_before_normalization(tmp_path, monkeypatch, original_sequence):
    write = pl.DataFrame.write_parquet
    def corrupt_selected_raw(frame, *args, **kwargs):
        if "point_kind" in frame.columns and frame.height:
            frame = frame.with_columns(pl.lit(original_sequence, dtype=pl.Int64).alias("refined_sequence_in_subtrack"))
        return write(frame, *args, **kwargs)
    monkeypatch.setattr(pl.DataFrame, "write_parquet", corrupt_selected_raw)
    with pytest.raises(local_refine.RefineParquetError, match="original identity"):
        cache(tmp_path, [("v", 1, [(0, 0, 0, 90)])], box(119.9, 29.9, 120.1, 30.1))


def test_injected_point_can_begin_a_valid_local_fragment(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    old = _published(original)
    parent = tmp_path / "parent"
    parent.mkdir()
    handle = publish_dataset.create_staging(parent, "refine", old.refine_id, uuid4())
    declared = {}
    with duckdb.connect() as connection:
        for name in ("kept_points", "injected_points"):
            source = original / old.relative_path / name / "part.parquet"
            destination = handle.path / name / "part.parquet"
            destination.parent.mkdir()
            # Only original kept point 1 is outside the true ROI; injected
            # sequence 1 becomes the head of the local fragment legitimately.
            sql = """
              COPY (SELECT * REPLACE (CASE WHEN point_id = 1 THEN 121.0 ELSE lon END AS lon)
                    FROM read_parquet($1)) TO $2 (FORMAT PARQUET)
            """
            connection.execute(sql, [str(source), str(destination)])
            declared[name] = [f"{name}/part.parquet"]
    descriptor = build_descriptor(handle, declared, {"version": 1})
    publish_dataset.publish(handle)
    runtime = runtime_config(duckdb_threads=1)
    result = local_refine.prepare_local_refine(parent, descriptor, "present", runtime,
        inputs(box(119.9, 29.9, 120.1, 30.1)), tmp_path / "cache", batch_size=1)
    level = result["levels"]["L1"]
    with local_refine.LocalRefineReader(level, runtime) as reader:
        tracks = list(reader.iter_tracks("public.trajectory_points"))
        assert len(tracks[0]) == 2
        rows = [row for batch in reader.iter_subtrack_batches("v1", 0) for row in batch]
    assert rows[0][12] == "injected"
    assert rows[0][4] == 0
    assert rows[0][15] == -1
    frame = pl.read_parquet(str(level["publication_root"]) + "/" + descriptor.relative_path + "/injected_points/points.parquet")
    assert frame["original_after_sequence_in_subtrack"].to_list() == [0]
    assert frame["original_refined_sequence_in_subtrack"].to_list() == [1]
