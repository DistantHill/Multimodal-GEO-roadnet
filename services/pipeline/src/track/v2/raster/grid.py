"""Fixed-anchor metric grid and deterministic boundary ownership."""

from __future__ import annotations

import math

from .models import GridSpec


def build_grid_spec(processing_srid: int, resolution_m: float,
                    bounds: tuple[float, float, float, float]) -> GridSpec:
    west, south, east, north = bounds
    if not all(math.isfinite(v) for v in bounds) or west > east or south > north:
        raise ValueError("raster bounds must be finite and ordered")
    if not math.isfinite(resolution_m) or resolution_m <= 0:
        raise ValueError("resolution_m must be finite and positive")
    # Fixed global anchor: floor minimum, include the maximum on its own grid line.
    left = math.floor(west / resolution_m)
    right = math.floor(east / resolution_m) + 1
    # The world interval is (south, north]; an integer minimum needs one row below.
    bottom = math.ceil(south / resolution_m) - 1
    top = math.ceil(north / resolution_m)
    return GridSpec(processing_srid, left * resolution_m, top * resolution_m,
                    right - left, top - bottom, resolution_m)


def explicit_grid_spec(processing_srid: int, resolution_m: float,
                       bounds: tuple[float, float, float, float]) -> GridSpec:
    west, south, east, north = bounds
    if (not all(math.isfinite(v) for v in bounds) or
            not (west < east and south < north)):
        raise ValueError("explicit raster bounds must have positive finite area")
    if not math.isfinite(resolution_m) or resolution_m <= 0:
        raise ValueError("resolution_m must be finite and positive")

    def pixel_index(coordinate: float) -> float:
        index = coordinate / resolution_m
        if not math.isfinite(index):
            raise ValueError("raster pixel indices must be finite")
        nearest = round(index)
        return nearest if math.isclose(index, nearest, rel_tol=0, abs_tol=1e-9) else index

    left, bottom = math.floor(pixel_index(west)), math.floor(pixel_index(south))
    right, top = math.ceil(pixel_index(east)), math.ceil(pixel_index(north))
    # A positive sub-tolerance box must still produce a nonempty grid.
    if right <= left:
        left, right = math.floor(west / resolution_m), math.ceil(east / resolution_m)
    if top <= bottom:
        bottom, top = math.floor(south / resolution_m), math.ceil(north / resolution_m)
    return GridSpec(processing_srid, left * resolution_m, top * resolution_m,
                    right - left, top - bottom, resolution_m)


def point_to_cell(grid: GridSpec, x: float, y: float) -> tuple[int, int] | None:
    if not math.isfinite(x) or not math.isfinite(y):
        return None
    col = math.floor((x - grid.west) / grid.resolution_m)
    row = math.floor((grid.north - y) / grid.resolution_m)
    if 0 <= col < grid.width_px and 0 <= row < grid.height_px:
        return row, col
    return None


def cell_center(grid: GridSpec, row: int, col: int) -> tuple[float, float]:
    return (grid.west + (col + 0.5) * grid.resolution_m,
            grid.north - (row + 0.5) * grid.resolution_m)


def pixel_to_world(grid: GridSpec, col: float, row: float) -> tuple[float, float]:
    return grid.west + col * grid.resolution_m, grid.north - row * grid.resolution_m
