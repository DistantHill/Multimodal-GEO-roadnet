from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely import from_wkb

from pipeline.artifacts import ParquetSink, SOURCE_FIELDS, schema
from pipeline.config import DEFAULTS, validate_config
from pipeline.resources import ResourceLimitExceeded
from pipeline.source import connection, iter_groups, prepare_source


def source_config(path: Path) -> dict:
    config = deepcopy(DEFAULTS)
    config["input"].update(kind="csv", path=str(path), source_crs="EPSG:32650", processing_crs="EPSG:32650")
    config["resources"].update(batch_rows=2, threads=1, duckdb_memory_mb=128)
    return validate_config(config)


def test_normalized_snapshot_is_sorted_self_contained_and_retains_extra_attributes(tmp_path: Path) -> None:
    source = tmp_path / "input.csv"
    source.write_text(
        "id,vehicle_id,timestamp,lon,lat,heading,speed,cargo\n"
        "p2,v1,2026-10-23T08:01:00,500010,3500000,450,36,steel\n"
        "bad,v1,2026-10-23T08:02:00,not_a_number,3500000,90,36,steel\n"
        "p1,v1,2026-10-23T08:00:00,500000,3500000,90,36,wood\n"
        "p3,v2,2026-10-23T08:01:00,500000,3500010,0,18,food\n"
    )
    result = prepare_source(source_config(source), tmp_path / "runs")
    rows = pq.read_table(result.path).to_pylist()
    assert result.rows == 3 and result.rejected_rows == 1
    assert [row["source_id"] for row in rows] == ["p1", "p2", "p3"]
    assert [row["source_seq"] for row in rows] == [0, 1, 0]
    assert len({row["source_point_id"] for row in rows}) == 3
    first = rows[0]
    assert first["speed"] == pytest.approx(10.0)
    assert rows[1]["heading"] == pytest.approx(90.0)
    assert first["timestamp"] == datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc).timestamp()
    assert from_wkb(first["geometry"]).x == 500000
    assert first["lon"] == pytest.approx(117.0, abs=0.01)
    assert json.loads(first["attributes_json"])["cargo"] == "wood"
    assert set(pq.read_schema(result.path).names) == {name for name, _ in SOURCE_FIELDS}
    geo = json.loads(pq.read_schema(result.path).metadata[b"geo"])
    assert geo["columns"]["geometry"]["crs"]["id"]["code"] == 32650
    rejected = pq.read_table(result.path.parent / "rejected.parquet").to_pylist()
    assert rejected[0]["source_id"] == "bad"
    assert json.loads(rejected[0]["attributes_json"])["lon"] == "not_a_number"


def test_input_snapshot_reuse_is_checksum_guarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "input.csv"
    source.write_text("id,vehicle_id,timestamp,lon,lat\np1,v1,2026-01-01,500000,3500000\n")
    config = source_config(source)
    result = prepare_source(config, tmp_path / "runs")
    import pipeline.source as module
    def unexpected(*args, **kwargs):
        pytest.fail("A verified cached source should not be reread")
    monkeypatch.setattr(module, "_raw_batches", unexpected)
    reused = prepare_source(config, tmp_path / "runs")
    assert reused.path == result.path and reused.identity == result.identity
    result.path.write_bytes(result.path.read_bytes() + b"corrupted")
    with pytest.raises(ValueError, match="checksum"):
        prepare_source(config, tmp_path / "runs")


def test_duplicate_primary_keys_never_publish_a_complete_source(tmp_path: Path) -> None:
    source = tmp_path / "duplicate.csv"
    source.write_text(
        "id,vehicle_id,timestamp,lon,lat\np1,v1,2026-01-01,500000,3500000\np1,v2,2026-01-02,500010,3500000\n"
    )
    output = tmp_path / "runs"
    with pytest.raises(ValueError, match="Duplicate source primary key"):
        prepare_source(source_config(source), output)
    assert not list(output.glob("inputs/*/manifest.json"))


def test_vehicle_group_crossing_arrow_batches_is_yielded_once(tmp_path: Path) -> None:
    table = pa.table({"vehicle_id": ["a"] * 5 + ["b"] * 2 + ["c"], "sequence": list(range(8))})
    path = tmp_path / "points.parquet"
    pq.write_table(table, path)
    config = source_config(path)
    with connection(config, tmp_path / "spill") as database:
        groups = list(iter_groups(database,
            "SELECT * FROM read_parquet(?) ORDER BY vehicle_id, sequence", [str(path)], ("vehicle_id",), 2, 5))
    assert [len(group) for group in groups] == [5, 2, 1]
    assert [row["sequence"] for group in groups for row in group] == list(range(8))


def test_group_limits_raise_instead_of_splitting_trajectories(tmp_path: Path) -> None:
    with duckdb.connect() as database:
        database.register("points", pa.table({"vehicle": ["a"] * 3, "xy": [[[0., 0.], [1., 1.]]] * 3}))
        with pytest.raises(ResourceLimitExceeded, match="maximum of 2 rows"):
            list(iter_groups(database, "SELECT * FROM points ORDER BY vehicle", [], ("vehicle",), 1, 2))
        with pytest.raises(ResourceLimitExceeded, match="maximum of 5 points"):
            list(iter_groups(database, "SELECT * FROM points ORDER BY vehicle", [], ("vehicle",), 1, 5, weight_field="xy"))


def test_empty_and_failed_artifact_writes_do_not_claim_completed_data(tmp_path: Path) -> None:
    empty = tmp_path / "empty.parquet"
    with ParquetSink(empty, schema(SOURCE_FIELDS, "EPSG:32650", "Point")):
        pass
    assert pq.ParquetFile(empty).metadata.num_rows == 0
    failed = tmp_path / "failed.parquet"
    with pytest.raises(ValueError, match="abort"):
        with ParquetSink(failed, schema(SOURCE_FIELDS, "EPSG:32650", "Point")):
            raise ValueError("abort")
    assert not failed.exists()
    assert not failed.with_name("failed.parquet.partial").exists()


def _prepare_source_process(config: dict, output_root: str, role: str, channel) -> None:
    """Real independent importer; barriers make publication overlap deterministic."""
    import os
    import traceback
    import pipeline.source as module

    original_reader = module._raw_batches

    def reader(*args, **kwargs):
        if role != "first":
            raise AssertionError("A waiting importer reread the source instead of reusing the published manifest")
        channel.send(("reader_entered", os.getpid()))
        if not channel.poll(15) or channel.recv() != "release":
            raise TimeoutError("The importer publication barrier was not released")
        yield from original_reader(*args, **kwargs)

    module._raw_batches = reader
    if role == "second":
        original_flock = module.fcntl.flock

        def observed_flock(handle, operation):
            if operation & module.fcntl.LOCK_EX:
                channel.send(("lock_attempted", os.getpid()))
            return original_flock(handle, operation)

        module.fcntl.flock = observed_flock
    try:
        result = module.prepare_source(config, Path(output_root))
        channel.send(("complete", str(result.path), result.identity))
    except BaseException:
        channel.send(("error", traceback.format_exc()))
        raise
    finally:
        channel.close()


def test_concurrent_process_waits_then_reuses_complete_input_snapshot(tmp_path: Path) -> None:
    import multiprocessing

    from pipeline.artifacts import digest, sha256

    source = tmp_path / "input.csv"
    source.write_text(
        "id,vehicle_id,timestamp,lon,lat\n"
        "p2,v1,2026-01-02,500010,3500000\n"
        "p1,v1,2026-01-01,500000,3500000\n"
    )
    config = source_config(source)
    output = tmp_path / "runs"
    original_identity = {
        "input": config["input"], "schema_version": 1,
        "files": [{"path": str(source.resolve()), "bytes": source.stat().st_size, "sha256": sha256(source)}],
    }
    expected_path = output / "inputs" / digest(original_identity) / "points.parquet"
    context = multiprocessing.get_context("spawn")
    first_channel, first_child = context.Pipe()
    second_channel, second_child = context.Pipe()
    first = context.Process(target=_prepare_source_process, args=(config, str(output), "first", first_child))
    second = context.Process(target=_prepare_source_process, args=(config, str(output), "second", second_child))
    try:
        first.start()
        first_child.close()
        assert first_channel.poll(15), "First importer did not reach its normalization barrier"
        message = first_channel.recv()
        assert message[0] == "reader_entered", message
        assert not expected_path.with_name("manifest.json").exists()
        second.start()
        second_child.close()
        assert second_channel.poll(15), "Second importer did not attempt the cache lock"
        message = second_channel.recv()
        assert message[0] == "lock_attempted", message
        # The first importer holds a real process lock while its partial files exist.
        assert second.is_alive()
        assert not second_channel.poll(0.2), "Second importer did not wait for atomic publication"
        first_channel.send("release")
        assert first_channel.poll(15), "First importer failed to complete"
        first_result = first_channel.recv()
        assert first_result[0] == "complete", first_result
        assert second_channel.poll(15), "Waiting importer failed to reuse the completed snapshot"
        second_result = second_channel.recv()
        assert second_result[0] == "complete", second_result
        first.join(5)
        second.join(5)
        assert first.exitcode == second.exitcode == 0
        assert first_result[1] == second_result[1] == str(expected_path)
        assert first_result[2] == second_result[2]
        assert json.loads(expected_path.with_name("manifest.json").read_text())["status"] == "COMPLETE"
        assert [row["source_id"] for row in pq.read_table(expected_path).to_pylist()] == ["p1", "p2"]
        assert not list(expected_path.parent.glob("*.partial"))
        assert not list(expected_path.parent.glob("normalizing-*.parquet"))
        assert expected_path.with_name("prepare.lock").is_file()
    finally:
        for process in (first, second):
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(5)
        for channel in (first_channel, first_child, second_channel, second_child):
            channel.close()
