"""Conservative integer point counts."""

from __future__ import annotations

import numpy as np

from .grid import point_to_cell
from .models import GridSpec


def allocate_counts(grid: GridSpec, *, max_cells: int, memory_budget_bytes: int,
                    tile_size_px: int = 1024) -> np.ndarray:
    cells = grid.width_px * grid.height_px
    # Counts, quantile workspace, uint32 conversion, gray and optional RGB.
    estimate = cells * 40 + tile_size_px * tile_size_px * 4
    if cells > max_cells or estimate > memory_budget_bytes:
        raise ValueError(f"raster exceeds resource limit: {cells} cells, estimated {estimate} bytes")
    return np.zeros((grid.height_px, grid.width_px), dtype=np.uint64)


def accumulate_raw_counts(counts: np.ndarray, grid: GridSpec,
                          points: np.ndarray) -> tuple[int, int]:
    accepted = excluded = 0
    maximum = np.iinfo(np.uint64).max
    for x, y in points:
        cell = point_to_cell(grid, float(x), float(y))
        if cell is None:
            excluded += 1
            continue
        row, col = cell
        if counts[row, col] == maximum:
            raise OverflowError("uint64 raw count overflow")
        counts[row, col] += 1
        accepted += 1
    return accepted, excluded


def validate_count_conservation(counts: np.ndarray, accepted: int) -> None:
    if sum(int(v) for v in counts.flat) != accepted:
        raise AssertionError("raw count conservation failed")
