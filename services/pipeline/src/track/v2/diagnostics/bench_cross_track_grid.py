"""Read-only single-grid benchmark; no Detect Run or PostgreSQL writes."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import resource
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import UUID

from ..candidates.cross_track_timing import CrossTrackTiming
from ..candidates.detect_grid_crossings import (
    iter_grid_candidates, iter_grid_crossings, iter_spilled_grid_candidates,
)
from ..candidates.grid import create_grid
from ..candidates.grid_segments import GridSegmentRelation, open_grid_segments
from ..config import connect
from ..cli._load_config import load_startup_config
from ..db.artifact_registry import find_refine_artifact
from ..db.fetch_refine_points_in_grid import fetch_points_in_grid
from ..projection import CoordinateTransformer, spatial_context_from_values
from ..trajectories.results.read_parquet import RefineDatasetReader


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refine-id", type=UUID, required=True)
    parser.add_argument("--grid", type=int, nargs=2, default=(0, 0))
    parser.add_argument("--batch-sizes", type=int, nargs="+", default=(10000, 100000, 500000))
    parser.add_argument("--min-avg-speed", type=float, default=None)
    args = parser.parse_args()
    if any(size < 1 for size in args.batch_sizes):
        parser.error("batch sizes must be positive")
    cfg = load_startup_config()
    detect = cfg.detect
    if args.min_avg_speed is not None:
        import math
        if not math.isfinite(args.min_avg_speed) or args.min_avg_speed < 0:
            parser.error("minimum average speed must be finite and non-negative")
        detect = replace(detect, cross_track_min_avg_speed=args.min_avg_speed)
    with TemporaryDirectory(prefix="cross-grid-bench-") as temporary, connect(cfg.postgres) as conn:
        root = Path(temporary)
        conn.execute("SET TRANSACTION READ ONLY")
        row = conn.execute(
            "SELECT source_srid, processing_srid, projection_config FROM refine_runs WHERE id=%s",
            (args.refine_id,),
        ).fetchone()
        if row is None:
            parser.error("unknown Refine Run in the configured database")
        context = spatial_context_from_values(*row)
        transformer = CoordinateTransformer(context)
        with conn.cursor() as cursor:
            registered = find_refine_artifact(cursor, args.refine_id)
        with RefineDatasetReader(cfg.paths.parquet_directory, registered.descriptor,
                                 registered.state, cfg.runtime, data_root=root) as reader:
            grids = create_grid(reader.extent(), detect.cross_track_task_grid_m)
        col, row = args.grid
        if col < 0 or row < 0 or col >= len(grids) or row >= len(grids[col]):
            parser.error("grid index outside the Refine extent")
        timing = CrossTrackTiming()
        with open_grid_segments(
            fetch_points_in_grid(conn, (col, row), grids, args.refine_id, context),
            max_segments=200000, max_bytes=268435456, runtime=cfg.runtime,
            data_root=root, bounds=grids[col][row], timing=timing,
        ) as segments:
            spilled = isinstance(segments, GridSegmentRelation)
            print(json.dumps({"phase": "prepare",
                              "refine_id": str(args.refine_id), "grid": args.grid,
                              "segments": segments.count if spilled else len(segments["subtrack_index"]),
                              "mode": "spill" if spilled else "memory", **timing.values}), flush=True)
            for batch_size in args.batch_sizes:
                selected = replace(detect, cross_track_pair_batch_size=batch_size)
                timing = CrossTrackTiming()
                started = perf_counter()
                if spilled:
                    batches = iter_spilled_grid_candidates(segments, selected, transformer, timing=timing)
                else:
                    crossings = iter_grid_crossings(segments, selected, max_grid_memberships=2000000,
                                                    max_overflow_pair_space=20000000, timing=timing)
                    batches = iter_grid_candidates(segments, crossings, transformer, timing=timing)
                count = sum(len(batch) for batch in batches)
                print(json.dumps({"phase": "compute", "batch_size": batch_size,
                                  "min_avg_speed": selected.cross_track_min_avg_speed,
                                  "elapsed_s": perf_counter() - started, "candidates": count,
                                  "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                                  **timing.values}), flush=True)


if __name__ == "__main__":
    main()
