"""Configuration and result types for point rasterization."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RasterConfig:
    resolution_m: float = 1.0
    count_mode: str = "raw"
    point_kernel: str = "none"
    clip_quantile: float = 0.95
    quantile_method: str = "linear"
    normalizer: str = "sqrt"
    channel_encoder: str = "gray_repeat"
    tile_size_px: int = 1024
    overlap_px: int = 256
    edge_mode: str = "pad"
    materialize_rgb_geotiff: bool = False
    stream_batch_size: int = 10000
    block_size_px: int = 512
    max_cells: int = 100000000
    memory_budget_bytes: int = 1073741824

    def __post_init__(self) -> None:
        if not math.isfinite(self.resolution_m) or self.resolution_m <= 0:
            raise ValueError("raster resolution_m must be finite and positive")
        for name, expected in (("count_mode", "raw"), ("point_kernel", "none"),
                               ("quantile_method", "linear"), ("normalizer", "sqrt"),
                               ("channel_encoder", "gray_repeat"), ("edge_mode", "pad")):
            if getattr(self, name) != expected:
                raise ValueError(f"unsupported raster {name}: {getattr(self, name)}")
        if self.clip_quantile != 0.95:
            raise ValueError("raster clip_quantile must be 0.95 in MVP")
        if self.tile_size_px <= 0 or not 0 <= self.overlap_px < self.tile_size_px:
            raise ValueError("raster tile_size_px must exceed overlap_px >= 0")
        if min(self.stream_batch_size, self.block_size_px, self.max_cells,
               self.memory_budget_bytes) <= 0:
            raise ValueError("raster resource limits must be positive")

    def semantic_dict(self) -> dict[str, object]:
        data = asdict(self)
        for key in ("stream_batch_size", "block_size_px", "max_cells", "memory_budget_bytes"):
            data.pop(key)
        return data

    def execution_dict(self) -> dict[str, int]:
        return {key: getattr(self, key) for key in
                ("stream_batch_size", "block_size_px", "max_cells", "memory_budget_bytes")}


@dataclass(frozen=True, slots=True)
class GridSpec:
    processing_srid: int
    west: float
    north: float
    width_px: int
    height_px: int
    resolution_m: float

    @property
    def east(self) -> float:
        return self.west + self.width_px * self.resolution_m

    @property
    def south(self) -> float:
        return self.north - self.height_px * self.resolution_m

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "east": self.east, "south": self.south,
                "gdal_geotransform": [self.west, self.resolution_m, 0,
                                      self.north, 0, -self.resolution_m]}


@dataclass(frozen=True, slots=True)
class RasterRunRequest:
    refine_id: str
    output_root: Path
    bounds: tuple[float, float, float, float] | None = None  # west, south, east, north
    time_start: str | None = None
    time_end: str | None = None


@dataclass(frozen=True, slots=True)
class RasterRunResult:
    raster_run_id: str
    output_directory: Path
    grid: GridSpec
    input_point_count: int
    accepted_point_count: int
    excluded_point_count: int
