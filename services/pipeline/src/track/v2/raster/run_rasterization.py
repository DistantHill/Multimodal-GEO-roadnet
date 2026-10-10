"""One read of Refine points followed by verified, isolated raster publication."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import struct
import tempfile
import time
import uuid
from pathlib import Path
from typing import Iterable

import numpy as np

from .aggregate_point_counts import accumulate_raw_counts, allocate_counts, validate_count_conservation
from .artifacts import verify_geotiff, write_geotiff, write_json
from .grid import build_grid_spec, explicit_grid_spec
from .models import RasterConfig, RasterRunRequest, RasterRunResult
from .read_refine_points import iter_refined_points, load_refine_spatial_context
from .render_counts_to_uint8 import fit_q95_profile, render_sqrt_uint8
from .write_rgb_tiles import write_tiles


def _spool_points(batches: Iterable[list[tuple[float, float]]], path: Path) -> tuple[int, int, tuple[float, float, float, float] | None]:
    count = invalid = 0
    xmin = ymin = math.inf
    xmax = ymax = -math.inf
    with path.open("wb") as stream:
        for batch in batches:
            for x, y in batch:
                count += 1
                if not math.isfinite(x) or not math.isfinite(y):
                    invalid += 1
                    continue
                stream.write(struct.pack("<dd", x, y))
                xmin, ymin = min(xmin, x), min(ymin, y)
                xmax, ymax = max(xmax, x), max(ymax, y)
    return count, invalid, None if xmin == math.inf else (xmin, ymin, xmax, ymax)


def _spooled_batches(path: Path, batch_size: int) -> Iterable[np.ndarray]:
    with path.open("rb") as stream:
        while data := stream.read(batch_size * 16):
            yield np.frombuffer(data, dtype="<f8").reshape(-1, 2)


def _estimate_disk_bytes(width: int, height: int, config: RasterConfig) -> int:
    cells = width * height
    stride = config.tile_size_px - config.overlap_px
    def axis_count(length: int) -> int:
        return 1 if length <= config.tile_size_px else 1 + math.ceil(
            (length - config.tile_size_px) / stride)
    tiles = axis_count(width) * axis_count(height)
    # Uncompressed TIFF bands and RGB/mask PNG payloads, plus file overhead.
    return math.ceil((cells * (8 if config.materialize_rgb_geotiff else 5)
                      + tiles * config.tile_size_px ** 2 * 4) * 1.1)


def run_rasterization(conn: object, request: RasterRunRequest,
                      config: RasterConfig) -> RasterRunResult:
    """Create complete artifacts after a single named-cursor database scan."""
    from ..db.pipeline_store import PostgresPipelineStore
    from ..time_selection import validate_refine_time
    store = PostgresPipelineStore(conn)
    start = end = None
    if request.time_start or request.time_end:
        start, end = validate_refine_time(store, store.get_refine_run(request.refine_id),
                                        request.time_start, request.time_end)
    run_id = str(uuid.uuid4())
    identities = {name: str(uuid.uuid5(uuid.UUID(run_id), name)) for name in (
        "grid_id", "count_artifact_id", "normalization_id", "render_id", "tile_set_id")}
    root = request.output_root.resolve()
    if root == Path("/"):
        raise ValueError("raster output root cannot be filesystem root")
    final = root / "raster" / run_id
    staging_parent = root / ".artifact-staging" / "raster"
    staging_parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"{run_id}-", dir=staging_parent) as scratch:
        staging = Path(scratch)
        write_json(staging / "run_manifest.json", {"raster_run_id": run_id,
                   "refine_id": request.refine_id, "status": "running"})
        try:
            source_table, spatial_context, source_fingerprint = load_refine_spatial_context(
                conn, request.refine_id)
            srid = spatial_context.processing_srid
            spool = staging / "points.bin"
            input_count, invalid, inferred_bounds = _spool_points(
                iter_refined_points(conn, refine_id=request.refine_id,
                                    source_table=source_table,
                                    spatial_context=spatial_context,
                                    batch_size=config.stream_batch_size,
                                    **({"time_start": start, "time_end": end}
                                       if start is not None or end is not None else {})), spool)
            bounds = request.bounds or inferred_bounds
            if bounds is None:
                raise ValueError("empty Refine point stream needs explicit raster bounds")
            grid = (explicit_grid_spec(srid, config.resolution_m, bounds)
                    if request.bounds else build_grid_spec(srid, config.resolution_m, bounds))
            estimated_disk_bytes = _estimate_disk_bytes(
                grid.width_px, grid.height_px, config)
            if estimated_disk_bytes > shutil.disk_usage(staging).free:
                raise ValueError("raster output exceeds available disk space")
            counts = allocate_counts(grid, max_cells=config.max_cells,
                                     memory_budget_bytes=config.memory_budget_bytes,
                                     tile_size_px=config.tile_size_px)
            accepted = 0
            excluded = invalid
            for batch in _spooled_batches(spool, config.stream_batch_size):
                a, e = accumulate_raw_counts(counts, grid, batch)
                accepted += a
                excluded += e
            if accepted + excluded != input_count:
                raise AssertionError("raster point accounting failed")
            validate_count_conservation(counts, accepted)
            profile = fit_q95_profile(counts)
            gray = render_sqrt_uint8(counts, profile["q95"])
            write_geotiff(staging / "raw_count.tif", counts, grid,
                          block_size_px=config.block_size_px)
            verify_geotiff(staging / "raw_count.tif", counts, grid,
                           block_size_px=config.block_size_px)
            write_geotiff(staging / "gray_uint8.tif", gray, grid,
                          block_size_px=config.block_size_px)
            verify_geotiff(staging / "gray_uint8.tif", gray, grid,
                           block_size_px=config.block_size_px)
            artifacts = ["raw_count.tif", "gray_uint8.tif"]
            if config.materialize_rgb_geotiff:
                write_geotiff(staging / "rgb_uint8.tif", gray, grid,
                              block_size_px=config.block_size_px, repeat_rgb=True)
                verify_geotiff(staging / "rgb_uint8.tif", gray, grid,
                               block_size_px=config.block_size_px, repeat_rgb=True)
                artifacts.append("rgb_uint8.tif")
            tile_count = write_tiles(staging / "tiles", gray, grid,
                                     tile_size_px=config.tile_size_px,
                                     overlap_px=config.overlap_px,
                                     grid_id=identities["grid_id"],
                                     count_artifact_id=identities["count_artifact_id"],
                                     normalization_id=identities["normalization_id"],
                                     render_id=identities["render_id"],
                                     tile_set_id=identities["tile_set_id"])
            write_json(staging / "grid.json", {"grid_id": identities["grid_id"],
                       **grid.as_dict()})
            write_json(staging / "normalization.json", {
                       "normalization_id": identities["normalization_id"],
                       "count_artifact_id": identities["count_artifact_id"], **profile})
            write_json(staging / "count_metadata.json", {
                       "count_artifact_id": identities["count_artifact_id"],
                       "grid_id": identities["grid_id"], "dtype": "uint32",
                       "count_mode": "raw", "nodata": None,
                       "total_count": accepted, "max_cell_count": int(counts.max())})
            write_json(staging / "render_metadata.json", {
                       "render_id": identities["render_id"],
                       "normalization_id": identities["normalization_id"],
                       "grid_id": identities["grid_id"],
                       "gray_geotiff": "gray_uint8.tif",
                       "rgb_geotiff": ("rgb_uint8.tif" if config.materialize_rgb_geotiff
                                       else None),
                       "channel_encoder": "gray_repeat"})
            write_json(staging / "tiles" / "metadata.json", {
                       "tile_set_id": identities["tile_set_id"],
                       "render_id": identities["render_id"],
                       "grid_id": identities["grid_id"],
                       "count_artifact_id": identities["count_artifact_id"],
                       "normalization_id": identities["normalization_id"],
                       "tile_size_px": config.tile_size_px,
                       "overlap_px": config.overlap_px,
                       "tile_count": tile_count,
                       "index": "tiles.csv"})
            write_json(staging / "metrics.json", {"input_point_count": input_count,
                       "accepted_point_count": accepted, "excluded_point_count": excluded,
                       "invalid_coordinate_count": invalid, "tile_count": tile_count,
                       "elapsed_seconds": time.monotonic() - started,
                       "estimated_memory_bytes": grid.width_px * grid.height_px * 40
                       + config.tile_size_px * config.tile_size_px * 4,
                       "estimated_disk_bytes": estimated_disk_bytes})
            semantic = config.semantic_dict()
            digest = hashlib.sha256(json.dumps(semantic, sort_keys=True).encode()).hexdigest()
            write_json(staging / "run_manifest.json", {
                "raster_run_id": run_id, "refine_id": request.refine_id,
                "source_table": source_table, "source_srid": spatial_context.source_srid,
                "source_fingerprint": source_fingerprint,
                "time_start": start.isoformat() if start else None,
                "time_end": end.isoformat() if end else None,
                "processing_srid": srid, "projection_config": spatial_context.projection_config,
                "status": "complete", **identities, "semantic_config": semantic,
                "semantic_config_digest": digest,
                "execution_config": config.execution_dict(),
                "bounds": list(bounds),
                "effective_bounds": [grid.west, grid.south, grid.east, grid.north],
                "artifacts": artifacts + ["grid.json",
                "normalization.json", "count_metadata.json", "render_metadata.json",
                "metrics.json", "tiles/metadata.json", "tiles/tiles.csv"],
            })
            spool.unlink()
            final.parent.mkdir(parents=True, exist_ok=True)
            os.rename(staging, final)
            return RasterRunResult(run_id, final, grid, input_count, accepted, excluded)
        except Exception:
            # Keep a small status record while the incomplete staging tree is removed.
            failure = {"raster_run_id": run_id, "refine_id": request.refine_id,
                       "status": "failed"}
            try:
                failure_dir = root / "raster" / "failed"
                failure_dir.mkdir(parents=True, exist_ok=True)
                write_json(failure_dir / f"{run_id}.json", failure)
            except OSError:
                pass
            raise
