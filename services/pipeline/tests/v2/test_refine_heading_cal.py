"""Heading geometry and length filtering behavior, including direct file output."""
import json
from uuid import uuid4

import duckdb
import numpy as np
import polars as pl
from pyproj import CRS
import pytest
import shapely
from dataclasses import replace
from types import SimpleNamespace

from track.v2.artifacts.parquet_manifest import build_descriptor
from track.v2.artifacts.publish_dataset import create_staging, publish
from track.v2.trajectories.refine_trajectories import calculated_headings, refine_vehicle
from track.v2.trajectories.results.short_subtracks import ShortSubtrackWriter
from track.v2.trajectories.results.parquet_schema import REFINE_PARQUET_V2
from track.v2.trajectories.results.read_parquet import RefineDatasetReader
from track.v2.errors import ConfigError
from track.v2.config.pipeline_config_loader import load_pipeline_payload
from .config_factory import pipeline_config, refine_config, runtime_config
from .test_refine import batch, CONTEXT


def test_outgoing_heading_cardinals_repeated_points_and_terminal_direction():
    xy = np.array([[0, 0], [0, 1], [1, 1], [1, 0], [0, 0], [0, 0]], dtype=float)
    result = calculated_headings(xy)
    np.testing.assert_equal(result, [0, 90, 180, 270, np.nan, 270])
    assert np.isnan(calculated_headings(np.zeros((3, 2)))).all()


def test_heading_uses_final_injected_geometry_and_preserves_source_heading():
    source = batch([0, 10], headings=[90, 0], lonlat=[[116, 39], [116, 39]])
    original = source.heading.copy()
    result = refine_vehicle(source, spatial_context=CONTEXT,
                            config=refine_config(min_heading_delta_deg=45, max_heading_delta_deg=135))
    track = result.subtracks[0]
    assert track.kind.tolist() == ["p", "i", "p"]
    assert track.heading.tolist() == [90, 45, 0]
    np.testing.assert_array_equal(source.heading, original)
    assert np.isnan(track.heading_cal).all()
    assert not track.heading_cal.flags.writeable


@pytest.mark.parametrize("count", [9, 10, 11])
def test_length_filter_counts_kept_points_before_injection(count):
    source = batch(list(range(count)))
    result = refine_vehicle(source, spatial_context=CONTEXT,
                            config=refine_config(min_subtrack_points=10))
    assert len(result.subtracks) == (count >= 10)
    assert len(result.short_subtracks) == (count < 10)
    assert len(result.excluded) == 0
    if count < 10:
        short = result.short_subtracks[0]
        assert len(short) == count
        assert np.isnan(short.heading_cal).all()
        assert set(short.kind) == {"p"}


def test_stationary_removal_precedes_length_filter_without_overlapping_points():
    source = batch(list(range(12)), speeds=[0] * 4 + [10] * 8)
    result = refine_vehicle(source, spatial_context=CONTEXT,
                            config=refine_config(min_subtrack_points=11, max_stationary_duration_s=1))
    assert result.excluded.source_row_key.tolist() == [1, 2]
    short = result.short_subtracks[0]
    assert len(short) == 10
    assert not set(short.source_row_key) & set(result.excluded.source_row_key)
    assert not result.subtracks


def test_filtered_heading_change_does_not_generate_points():
    source = batch([0, 10], headings=[90, 0], lonlat=[[116, 39], [116, 39]])
    result = refine_vehicle(source, spatial_context=CONTEXT,
                            config=refine_config(min_subtrack_points=3))
    assert not result.subtracks
    assert len(result.short_subtracks[0]) == 2
    assert sum(result.derived_counts.values()) == 0


def test_heading_cal_tracks_nonzero_injected_segments():
    source = batch([0, 10], headings=[90, 0], lonlat=[[116.0001, 39], [116, 39.0001]])
    track = refine_vehicle(source, spatial_context=CONTEXT,
        config=refine_config(min_heading_delta_deg=45, max_heading_delta_deg=135)).subtracks[0]
    assert track.kind.tolist() == ["p", "i", "p"]
    deltas = np.diff(track.xy, axis=0)
    expected = np.degrees(np.arctan2(deltas[:, 0], deltas[:, 1])) % 360
    np.testing.assert_allclose(track.heading_cal, [*expected, expected[-1]])
    assert track.heading.tolist() == [90, 45, 0]


def test_stream_sends_only_normal_tracks_to_postgres_and_short_points_to_files(tmp_path, monkeypatch):
    from track.v2.stages import refine as stage
    short = batch(list(range(9)))
    long = replace(batch(list(range(10))), vehicle_id="long")
    class Handle:
        def count_rows(self):
            return 19
        def close(self):
            pass
    class Stream:
        def __init__(self, handle):
            self.handle = handle
        def iter_vehicle_tracks(self):
            yield short
            yield long
        def close(self):
            self.handle.close()
    persisted = []
    class Store:
        def persist_refine_batch(self, run_id, excluded, tracks, next_index, **kwargs):
            persisted.extend(tracks)
            result = SimpleNamespace(next_subtrack_index=next_index + len(tracks),
                                     kept_count=sum(np.count_nonzero(t.kind == "p") for t in tracks))
            excluded.clear()
            tracks.clear()
            return result
    runtime = runtime_config(import_batch_size=2, duckdb_threads=1)
    handle = create_staging(tmp_path.resolve(), "refine", uuid4(), uuid4())
    writer = ShortSubtrackWriter(handle, runtime, CONTEXT)
    monkeypatch.setattr(stage, "get_database", lambda *args, **kwargs: Handle())
    monkeypatch.setattr(stage, "StreamVehicleSource", Stream)
    counts = stage._stream_and_persist(
        store=Store(), source=None, refine_id=str(handle.run_id), spatial_context=CONTEXT,
        parser_config=pipeline_config().parser, refine_config=refine_config(min_subtrack_points=10),
        runtime_config=runtime, progress=SimpleNamespace(update=lambda **kwargs: None),
        emit=lambda event: None, short_writer=writer,
    )
    writer.finish()
    assert counts[:4] == (19, 10, 0, 0)
    assert [track.vehicle_id for track in persisted] == ["long"]
    assert writer.point_count == 9 and writer.track_count == 1


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_min_subtrack_points_rejects_invalid_config(value):
    # Exercise the public payload loader using checked-in TOML structure.
    import tomllib
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "config"
    payload = tomllib.loads((root / "pipeline.toml").read_text())
    payload["refine"]["min_subtrack_points"] = value
    with pytest.raises(ConfigError, match="min_subtrack_points"):
        load_pipeline_payload(payload, base_directory=root, postgres_password="test-password")


def _files(tmp_path, *, empty=False):
    handle = create_staging(tmp_path.resolve(), "refine", uuid4(), uuid4())
    runtime = runtime_config(import_batch_size=2, duckdb_threads=1)
    writer = ShortSubtrackWriter(handle, runtime, CONTEXT)
    if not empty:
        result = refine_vehicle(batch([0, 1, 2]), spatial_context=CONTEXT,
                                config=refine_config(min_subtrack_points=10))
        writer.append(result.short_subtracks)
    files = writer.finish()
    return handle, runtime, writer, files


@pytest.mark.parametrize("empty", [False, True])
def test_short_geoparquet_is_bounded_readable_and_registered_with_normal_results(tmp_path, empty):
    handle, runtime, writer, files = _files(tmp_path, empty=empty)
    assert writer.point_count == (0 if empty else 3)
    assert len(files) == (1 if empty else 2)
    points = pl.concat([pl.read_parquet(handle.path / path) for path in files])
    assert "heading_cal" not in points.columns
    if not empty:
        np.testing.assert_array_equal(shapely.get_coordinates(shapely.from_wkb(points["geometry"].to_list())),
                                      points.select("lon", "lat").to_numpy())
        assert points["sequence_in_subtrack"].to_list() == [0, 1, 2]
    with duckdb.connect() as connection:
        for path in files:
            rows = connection.execute("SELECT key, value FROM parquet_kv_metadata(?)", [str(handle.path / path)]).fetchall()
            metadata = json.loads(dict(rows)[b"geo"])
            assert metadata["version"] == "1.1.0"
            assert CRS.from_json_dict(metadata["columns"]["geometry"]["crs"]).to_epsg() == 4326
        tables = {"short_subtrack": files}
        for name, columns in REFINE_PARQUET_V2.items():
            directory = handle.path / name
            directory.mkdir()
            path = directory / "empty.parquet"
            projection = ", ".join(f"NULL::{c.duckdb_type} AS {c.name}" for c in columns)
            connection.execute(f"COPY (SELECT {projection} WHERE false) TO ? (FORMAT PARQUET)", [str(path)])
            tables[name] = [path.relative_to(handle.path).as_posix()]
    descriptor = build_descriptor(handle, tables, {"refine_id": str(handle.run_id)})
    publish(handle)
    with RefineDatasetReader(tmp_path, descriptor, "present", runtime) as reader:
        assert list(reader.iter_tracks("source")) == []
        batches = list(reader.iter_short_subtrack_batches(batch_size=1))
        assert sum(map(len, batches)) == (0 if empty else 3)
        assert all(row["refine_id"] == str(handle.run_id) for rows in batches for row in rows)
