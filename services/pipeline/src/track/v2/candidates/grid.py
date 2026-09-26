"""Closed XY grid cells over one Refine extent."""
from __future__ import annotations

import math


class GridCell(tuple[float, float, float, float]):
    """A four-bound cell that also carries its mutable task status."""

    status: str

    def __new__(cls, bounds: tuple[float, float, float, float]) -> GridCell:
        cell = super().__new__(cls, bounds)
        cell.status = "todo"
        return cell

    @property
    def aabb(self) -> tuple[float, float, float, float]:
        return tuple(self)


def create_grid(
    extent: tuple[float, float, float, float] | None,
    cell_size_m: float,
) -> list[list[GridCell]]:
    """Build ``grids[col][row]`` over Refine's processing-XY extent.

    Each cell has a closed AABB. Adjacent cells therefore share boundaries,
    and ``GridCell`` can be unpacked directly by ``fetch_points_in_grid``.
    An empty Refine extent gives an empty grid.
    """
    if not math.isfinite(cell_size_m) or cell_size_m <= 0:
        raise ValueError("cell_size_m must be finite and positive")
    if extent is None:
        return []
    x_min, y_min, x_max, y_max = extent
    if not all(math.isfinite(v) for v in extent):
        raise ValueError("extent must be finite")
    if x_min > x_max or y_min > y_max:
        raise ValueError("extent bounds are reversed")

    cols = max(1, math.ceil((x_max - x_min) / cell_size_m))
    rows = max(1, math.ceil((y_max - y_min) / cell_size_m))
    return [
        [
            GridCell((
                x_min + col * cell_size_m,
                y_min + row * cell_size_m,
                min(x_min + (col + 1) * cell_size_m, x_max),
                min(y_min + (row + 1) * cell_size_m, y_max),
            ))
            for row in range(rows)
        ]
        for col in range(cols)
    ]
