from __future__ import annotations

import csv
import json
import uuid

import numpy as np
import pytest
from PIL import Image

from track.v2.raster.aggregation import (
    accumulate_raw_counts, allocate_counts, validate_count_conservation,
)
from track.v2.raster.grid import build_grid_spec, cell_center, explicit_grid_spec, point_to_cell
from track.v2.raster.models import GridSpec, RasterConfig
from track.v2.raster.normalization import fit_q95_profile, render_sqrt_uint8
from track.v2.raster.tiling import iter_tile_windows, write_tiles


def test_six_points_global_q95_and_batch_order():
    grid = GridSpec(32649, 0, 2, 3, 2, 1)
    points = np.array([(0.5, 1.5)] * 4 + [(1.5, 1.5), (2.5, 1.5)])
    first = allocate_counts(grid, max_cells=6, memory_budget_bytes=10000, tile_size_px=4)
    second = allocate_counts(grid, max_cells=6, memory_budget_bytes=10000, tile_size_px=4)
    assert accumulate_raw_counts(first, grid, points) == (6, 0)
    for batch in (points[4:][::-1], points[:4]):
        accumulate_raw_counts(second, grid, batch)
    np.testing.assert_array_equal(first, second)
    validate_count_conservation(first, 6)
    profile = fit_q95_profile(first)
    assert profile["q95"] == pytest.approx(3.7)
    gray = render_sqrt_uint8(first, profile["q95"])
    assert gray[0].tolist() == [255, 133, 133]
    assert gray[1].tolist() == [0, 0, 0]


def test_grid_boundaries_and_auto_single_point():
    grid = explicit_grid_spec(32649, 1, (0, 0, 2, 2))
    assert point_to_cell(grid, 0, 2) == (0, 0)
    assert point_to_cell(grid, 1, 1) == (1, 1)
    assert point_to_cell(grid, 2, 1) is None
    assert point_to_cell(grid, 1, 0) is None
    assert point_to_cell(grid, float("nan"), 1) is None
    assert point_to_cell(grid, 1, float("inf")) is None
    assert cell_center(grid, 1, 1) == (1.5, 0.5)
    auto = build_grid_spec(32649, 1, (1, 1, 1, 1))
    assert (auto.width_px, auto.height_px) == (1, 1)
    assert point_to_cell(auto, 1, 1) == (0, 0)


def test_empty_and_count_overflow_guard(tmp_path):
    from track.v2.raster.artifacts import write_geotiff

    grid = GridSpec(32649, 0, 1, 1, 1, 1)
    empty = np.zeros((1, 1), dtype=np.uint64)
    assert fit_q95_profile(empty)["q95"] is None
    assert render_sqrt_uint8(empty, None).tolist() == [[0]]
    empty[0, 0] = np.iinfo(np.uint32).max + 1
    with pytest.raises(OverflowError):
        write_geotiff(tmp_path / "overflow.tif", empty, grid, block_size_px=16)


def test_tiling_mask_and_coordinates(tmp_path):
    grid = GridSpec(32649, 10, 20, 6, 5, 1)
    gray = np.arange(30, dtype=np.uint8).reshape(5, 6)
    assert list(iter_tile_windows(grid, 4, 1)) == [
        (0, 0, 4, 4), (0, 3, 4, 3), (3, 0, 2, 4), (3, 3, 2, 3),
    ]
    ids = {name: str(uuid.uuid4()) for name in (
        "grid_id", "count_artifact_id", "normalization_id", "render_id", "tile_set_id")}
    assert write_tiles(tmp_path / "tiles", gray, grid, tile_size_px=4, overlap_px=1,
                       **ids) == 4
    with (tmp_path / "tiles" / "tiles.csv").open(encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    last = rows[-1]
    assert tuple(float(last[key]) for key in (
        "valid_west", "valid_north", "valid_east", "valid_south")) == (
        13, 17, 16, 15)
    assert tuple(float(last[key]) for key in (
        "full_west", "full_north", "full_east", "full_south")) == (
        13, 17, 17, 13)
    assert (last["row_offset_px"], last["col_offset_px"],
            last["valid_height_px"], last["valid_width_px"],
            last["tile_height_px"], last["tile_width_px"]) == (
        "3", "3", "2", "3", "4", "4")
    assert json.loads(last["tile_transform_gdal"]) == [13, 1, 0, 17, 0, -1]
    assert all(last[key] == value for key, value in ids.items())
    mask = np.asarray(Image.open(tmp_path / "tiles" / last["mask"]))
    image = np.asarray(Image.open(tmp_path / "tiles" / last["image"]))
    assert mask.tolist() == [[255, 255, 255, 0], [255, 255, 255, 0],
                             [0, 0, 0, 0], [0, 0, 0, 0]]
    np.testing.assert_array_equal(image[:, :, 0], image[:, :, 1])
    np.testing.assert_array_equal(image[:, :, 1], image[:, :, 2])


def test_unsupported_configuration():
    with pytest.raises(ValueError, match="count_mode"):
        RasterConfig(count_mode="unique_vehicle_cell")


def test_geotiff_window_io_and_repeated_rgb(tmp_path, monkeypatch):
    import rasterio

    from track.v2.raster.artifacts import verify_geotiff, write_geotiff

    grid = GridSpec(32649, 10, 20, 6, 5, 1)
    gray = np.arange(30, dtype=np.uint8).reshape(5, 6)
    calls = []
    real_open = rasterio.open

    class Proxy:
        def __init__(self, dataset):
            self.dataset = dataset

        def __enter__(self):
            self.dataset.__enter__()
            return self

        def __exit__(self, *args):
            return self.dataset.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.dataset, name)

        def write(self, data, index, *, window):
            calls.append(("write", index, window.width, window.height))
            return self.dataset.write(data, index, window=window)

        def read(self, index, *, window):
            calls.append(("read", index, window.width, window.height))
            return self.dataset.read(index, window=window)

    monkeypatch.setattr(rasterio, "open", lambda *args, **kwargs: Proxy(
        real_open(*args, **kwargs)))
    path = tmp_path / "rgb.tif"
    write_geotiff(path, gray, grid, block_size_px=2, repeat_rgb=True)
    verify_geotiff(path, gray, grid, block_size_px=2, repeat_rgb=True)
    assert len([call for call in calls if call[0] == "write"]) == 27
    assert len([call for call in calls if call[0] == "read"]) == 27
    assert all(width <= 2 and height <= 2 for _, _, width, height in calls)


def test_point_source_uses_detect_union_and_binds_refine_twice(monkeypatch):
    from track.v2.db.source_table import SourceTableContract
    from track.v2.models import SpatialContext
    from track.v2.raster import point_source

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def execute(self, query, params):
            self.query, self.params = query, params

        def fetchmany(self, _size):
            return []

    class Connection:
        def __init__(self):
            self.read_cursor = Cursor()

        def cursor(self, *, name=None):
            assert name.startswith("raster_points_")
            return self.read_cursor

    monkeypatch.setattr(point_source, "validate_source_table", lambda *_: SourceTableContract(
        "trajectory_points", '"trajectory_points"', "bigint", True))
    conn = Connection()
    context = SpatialContext(4326, 32649, {"coordinate_semantics_version": 2})
    assert list(point_source.iter_refined_points(
        conn, refine_id="refine-A", source_table="trajectory_points",
        spatial_context=context, batch_size=10)) == []
    assert "UNION ALL" in conn.read_cursor.query
    assert "kept_trajectory_points" in conn.read_cursor.query
    assert conn.read_cursor.params == (
        "trajectory_points", "refine-A", "public.injected_points", "refine-A")


def test_runner_writes_verified_geotiffs_and_publishes_complete(tmp_path, monkeypatch):
    import rasterio

    from track.v2.models import SpatialContext
    from track.v2.raster import runner
    from track.v2.raster.models import RasterRunRequest

    context = SpatialContext(4326, 32649, {"coordinate_semantics_version": 2})
    fingerprint = {"algorithm": "sha256", "digest": "sample"}
    monkeypatch.setattr(runner, "load_refine_spatial_context",
                        lambda *_: ("trajectory_points", context, fingerprint))
    points = [(0.5, 1.5)] * 4 + [(1.5, 1.5), (2.5, 1.5)]
    monkeypatch.setattr(runner, "iter_refined_points",
                        lambda *_args, **_kwargs: iter((points[:2], points[2:])))
    result = runner.run_rasterization(object(), RasterRunRequest("refine-A", tmp_path),
                                      RasterConfig(tile_size_px=4, overlap_px=1,
                                                   block_size_px=2,
                                                   materialize_rgb_geotiff=True))
    assert result.accepted_point_count == result.input_point_count == 6
    assert not list((tmp_path / ".artifact-staging" / "raster").iterdir())
    manifest = json.loads((result.output_directory / "run_manifest.json").read_text())
    assert manifest["status"] == "complete"
    assert manifest["source_fingerprint"] == fingerprint
    grid_meta = json.loads((result.output_directory / "grid.json").read_text())
    count_meta = json.loads((result.output_directory / "count_metadata.json").read_text())
    normalization = json.loads((result.output_directory / "normalization.json").read_text())
    render = json.loads((result.output_directory / "render_metadata.json").read_text())
    tile_set = json.loads((result.output_directory / "tiles" / "metadata.json").read_text())
    assert grid_meta["grid_id"] == count_meta["grid_id"] == render["grid_id"]
    assert count_meta["count_artifact_id"] == normalization["count_artifact_id"]
    assert normalization["normalization_id"] == render["normalization_id"]
    assert render["render_id"] == tile_set["render_id"]
    assert all(manifest[key] == value for key, value in (
        ("grid_id", grid_meta["grid_id"]),
        ("count_artifact_id", count_meta["count_artifact_id"]),
        ("normalization_id", normalization["normalization_id"]),
        ("render_id", render["render_id"]),
        ("tile_set_id", tile_set["tile_set_id"])))
    with (result.output_directory / "tiles" / "tiles.csv").open(encoding="utf-8") as stream:
        tile_rows = list(csv.DictReader(stream))
    assert all(row["tile_set_id"] == manifest["tile_set_id"] and
               row["grid_id"] == manifest["grid_id"] and
               row["count_artifact_id"] == manifest["count_artifact_id"] and
               row["normalization_id"] == manifest["normalization_id"] and
               row["render_id"] == manifest["render_id"] for row in tile_rows)
    with rasterio.open(result.output_directory / "raw_count.tif") as src:
        assert src.crs.to_epsg() == 32649
        assert src.dtypes == ("uint32",)
        assert src.read(1).sum() == 6
    with rasterio.open(result.output_directory / "rgb_uint8.tif") as src:
        assert src.count == 3
        np.testing.assert_array_equal(src.read(1), src.read(2))
        np.testing.assert_array_equal(src.read(2), src.read(3))


def test_failed_run_has_no_published_directory(tmp_path, monkeypatch):
    import json

    from track.v2.raster import runner
    from track.v2.raster.models import RasterRunRequest

    monkeypatch.setattr(runner, "load_refine_spatial_context",
                        lambda *_: (_ for _ in ()).throw(ValueError("bad run")))
    with pytest.raises(ValueError, match="bad run"):
        runner.run_rasterization(object(), RasterRunRequest("refine-A", tmp_path),
                                 RasterConfig())
    assert not list((tmp_path / "raster").iterdir()) or all(
        path.name == "failed" for path in (tmp_path / "raster").iterdir())
    failure_files = list((tmp_path / "raster" / "failed").glob("*.json"))
    assert len(failure_files) == 1
    assert json.loads(failure_files[0].read_text())["status"] == "failed"
