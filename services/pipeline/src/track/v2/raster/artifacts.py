"""GeoTIFF and JSON outputs for one raster run."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .models import GridSpec


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def _windows(grid: GridSpec, block_size_px: int):
    from rasterio.windows import Window

    if block_size_px <= 0:
        raise ValueError("block_size_px must be positive")
    for row in range(0, grid.height_px, block_size_px):
        for col in range(0, grid.width_px, block_size_px):
            height = min(block_size_px, grid.height_px - row)
            width = min(block_size_px, grid.width_px - col)
            yield row, col, Window(col, row, width, height)


def write_geotiff(path: Path, array: np.ndarray, grid: GridSpec, *,
                  block_size_px: int, repeat_rgb: bool = False) -> None:
    import rasterio
    from rasterio.transform import from_origin

    if array.ndim != 2:
        raise ValueError("GeoTIFF source must be a 2D array")
    if array.shape != (grid.height_px, grid.width_px):
        raise ValueError("GeoTIFF array and grid dimensions differ")
    bands = 3 if repeat_rgb else 1
    if repeat_rgb and array.dtype != np.uint8:
        raise ValueError("RGB GeoTIFF source must be uint8")
    if array.dtype == np.uint64:
        maximum = int(array.max()) if array.size else 0
        if maximum > np.iinfo(np.uint32).max:
            raise OverflowError("raw count exceeds uint32 GeoTIFF range")
    output_dtype = np.dtype("uint32") if array.dtype == np.uint64 else array.dtype
    options = {}
    if min(grid.width_px, grid.height_px) >= 16:
        block = max(16, (block_size_px // 16) * 16)
        options = {"tiled": True, "blockxsize": min(block, (grid.width_px // 16) * 16),
                   "blockysize": min(block, (grid.height_px // 16) * 16)}
    with rasterio.open(
        path, "w", driver="GTiff", height=grid.height_px, width=grid.width_px,
        count=bands, dtype=output_dtype, crs=f"EPSG:{grid.processing_srid}",
        transform=from_origin(grid.west, grid.north, grid.resolution_m,
                              grid.resolution_m), compress="deflate", **options,
    ) as dst:
        for row, col, window in _windows(grid, block_size_px):
            block = array[row:row + int(window.height), col:col + int(window.width)]
            if output_dtype != array.dtype:
                block = block.astype(output_dtype)
            for band in range(1, bands + 1):
                dst.write(block, band, window=window)


def verify_geotiff(path: Path, expected: np.ndarray, grid: GridSpec, *,
                   block_size_px: int, repeat_rgb: bool = False) -> None:
    import rasterio
    from rasterio.transform import from_origin

    bands = 3 if repeat_rgb else 1
    output_dtype = np.dtype("uint32") if expected.dtype == np.uint64 else expected.dtype
    with rasterio.open(path) as src:
        if (src.count != bands or src.width != grid.width_px or
                src.height != grid.height_px or
                any(np.dtype(dtype) != output_dtype for dtype in src.dtypes) or
                src.crs is None or src.crs.to_epsg() != grid.processing_srid or
                src.transform != from_origin(grid.west, grid.north,
                                             grid.resolution_m, grid.resolution_m)):
            raise RuntimeError(f"GeoTIFF verification failed: {path.name}")
        for row, col, window in _windows(grid, block_size_px):
            block = expected[row:row + int(window.height), col:col + int(window.width)]
            for band in range(1, bands + 1):
                if not np.array_equal(src.read(band, window=window), block):
                    raise RuntimeError(f"GeoTIFF verification failed: {path.name}")
