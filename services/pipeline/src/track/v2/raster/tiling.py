"""Padded RGB tiles, validity masks, and geographic index."""

from __future__ import annotations

import csv
import json
import uuid
from pathlib import Path
from typing import Iterator

import numpy as np
from PIL import Image

from .grid import pixel_to_world
from .models import GridSpec


def _starts(length: int, size: int, stride: int) -> list[int]:
    starts = [0]
    while starts[-1] + size < length:
        starts.append(starts[-1] + stride)
    return starts


def iter_tile_windows(grid: GridSpec, size: int, overlap: int) -> Iterator[tuple[int, int, int, int]]:
    stride = size - overlap
    for row in _starts(grid.height_px, size, stride):
        for col in _starts(grid.width_px, size, stride):
            yield row, col, min(size, grid.height_px - row), min(size, grid.width_px - col)


def write_tiles(directory: Path, gray: np.ndarray, grid: GridSpec,
                *, tile_size_px: int, overlap_px: int,
                grid_id: str, count_artifact_id: str,
                normalization_id: str, render_id: str, tile_set_id: str) -> int:
    images = directory / "images"
    masks = directory / "masks"
    images.mkdir(parents=True)
    masks.mkdir(parents=True)
    fields = ("tile_id", "tile_index", "tile_set_id", "grid_id",
              "count_artifact_id", "normalization_id", "render_id",
              "processing_srid", "image", "mask", "row_offset_px", "col_offset_px",
              "valid_row_offset_px", "valid_col_offset_px", "valid_height_px",
              "valid_width_px", "tile_height_px", "tile_width_px",
              "valid_west", "valid_south", "valid_east", "valid_north",
              "full_west", "full_south", "full_east", "full_north",
              "tile_transform_gdal")
    count = 0
    with (directory / "tiles.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row, col, height, width in iter_tile_windows(grid, tile_size_px, overlap_px):
            name = f"tile_r{row:08d}_c{col:08d}.png"
            rgb = np.zeros((tile_size_px, tile_size_px, 3), dtype=np.uint8)
            rgb[:height, :width, :] = gray[row:row + height, col:col + width, None]
            mask = np.zeros((tile_size_px, tile_size_px), dtype=np.uint8)
            mask[:height, :width] = 255
            Image.fromarray(rgb, mode="RGB").save(images / name)
            Image.fromarray(mask, mode="L").save(masks / name)
            west, north = pixel_to_world(grid, col, row)
            valid_east, valid_south = pixel_to_world(grid, col + width, row + height)
            full_east, full_south = pixel_to_world(
                grid, col + tile_size_px, row + tile_size_px)
            tile_id = str(uuid.uuid5(uuid.UUID(tile_set_id), f"{row}:{col}"))
            writer.writerow({
                "tile_id": tile_id, "tile_index": count, "tile_set_id": tile_set_id,
                "grid_id": grid_id, "count_artifact_id": count_artifact_id,
                "normalization_id": normalization_id, "render_id": render_id,
                "processing_srid": grid.processing_srid,
                "image": f"images/{name}", "mask": f"masks/{name}",
                "row_offset_px": row, "col_offset_px": col,
                "valid_row_offset_px": 0, "valid_col_offset_px": 0,
                "valid_height_px": height, "valid_width_px": width,
                "tile_height_px": tile_size_px, "tile_width_px": tile_size_px,
                "valid_west": west, "valid_south": valid_south,
                "valid_east": valid_east, "valid_north": north,
                "full_west": west, "full_south": full_south,
                "full_east": full_east, "full_north": north,
                "tile_transform_gdal": json.dumps([
                    west, grid.resolution_m, 0, north, 0, -grid.resolution_m]),
            })
            count += 1
    return count
