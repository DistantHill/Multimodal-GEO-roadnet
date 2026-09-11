"""Read-only PostgreSQL export for raw-point CV normalization comparisons."""

from __future__ import annotations

import argparse
from array import array
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path
import uuid

import numpy as np

from ..config import connect, default_postgres_config
from ..cv_preview import fit_profile, split_tiles, write_report
from ..db.queries import fetch_detect_row, fetch_intersections
from ..db.source_table import validate_source_table, verify_clean_source_fingerprint
from ..projection import CoordinateTransformer


def read_pixels(conn, quoted_table: str, srid: int, pixel_size: float,
                max_pixels: int) -> np.ndarray:
    """Aggregate original source geometry in SQL; never load a dense city raster."""
    sql = f"""
        SELECT floor(ST_X(ST_Transform(geom, %s)) / %s)::bigint AS ix,
               floor(ST_Y(ST_Transform(geom, %s)) / %s)::bigint AS iy,
               count(*)::bigint
          FROM {quoted_table}
         GROUP BY 1, 2
    """
    packed = array("q")
    with conn.cursor(name="cv_raw_pixel_counts") as cur:
        cur.itersize = 10000
        cur.execute(sql, (srid, pixel_size, srid, pixel_size))
        for ix, iy, count in cur:
            if ix is None or iy is None:
                raise ValueError("source contains NULL/empty geometry; raw input cannot be silently dropped")
            if len(packed) // 3 >= max_pixels:
                raise ValueError("occupied pixel limit exceeded; restrict the source or raise --max-pixels")
            packed.extend((ix, iy, count))
    if not packed:
        raise ValueError("source table has no original points")
    return np.frombuffer(packed, dtype=np.int64).reshape(-1, 3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detect-id", type=uuid.UUID, required=True)
    parser.add_argument("--database", default=None)
    parser.add_argument("--output", type=Path, required=True,
                        help="New directory outside the repository, under DATA_ROOT or /tmp")
    parser.add_argument("--pixel-size", type=float, default=2.0, help="metres per pixel (preview default: 2)")
    parser.add_argument("--tile-size", type=int, default=512, help="pixels per tile (preview default: 512)")
    parser.add_argument("--split-x", type=float, default=None,
                        help="metre easting boundary, snapped to tile; default: midpoint of occupied extent")
    parser.add_argument("--log-scale", type=float, default=1.0)
    parser.add_argument("--asinh-scale", type=float, default=10.0)
    parser.add_argument("--max-pixels", type=int, default=2_000_000)
    args = parser.parse_args()
    if not math.isfinite(args.pixel_size) or args.pixel_size <= 0:
        parser.error("--pixel-size must be finite and positive")
    if not 16 <= args.tile_size <= 2048 or args.max_pixels < 1:
        parser.error("tile size must be 16..2048 and max pixels must be positive")
    if args.split_x is not None and not math.isfinite(args.split_x):
        parser.error("--split-x must be finite")
    if not all(math.isfinite(s) and s > 0 for s in (args.log_scale, args.asinh_scale)):
        parser.error("curve scales must be finite and positive")
    output = args.output.resolve()
    package_root = Path(__file__).resolve().parents[4]
    repository = package_root.parents[1] if package_root.parent.name == "services" else package_root
    if output == repository or repository in output.parents:
        parser.error("runtime output must be outside the repository")
    if output.exists():
        parser.error("output directory already exists; choose a new path")
    config = default_postgres_config()
    if args.database:
        config = replace(config, database=args.database)
    print("Reading original trajectory points in a read-only snapshot...", flush=True)
    with connect(config) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '180s'")
        run = fetch_detect_row(conn, str(args.detect_id))
        if run.status != "completed":
            raise ValueError("preview requires a completed Detect Run")
        verify_clean_source_fingerprint(conn, run.clean_id)
        contract = validate_source_table(conn, run.source_table)
        with conn.cursor() as cur:
            cur.execute("SELECT source_fingerprint FROM clean_runs WHERE id = %s", (run.clean_id,))
            fingerprint = cur.fetchone()[0]
            cur.execute(f"SELECT count(*), min(timestamp), max(timestamp), ST_Extent(geom)::text "
                        f"FROM {contract.quoted_table}")
            source_count, time_min, time_max, extent = cur.fetchone()
        if source_count != fingerprint["row_count"]:
            raise ValueError("source row count differs from Clean snapshot; select a matching run/source")
        pixels = read_pixels(conn, contract.quoted_table, run.spatial_context.processing_srid,
                             args.pixel_size, args.max_pixels)
        transformer = CoordinateTransformer(run.spatial_context)
        circles = []
        for intersection in fetch_intersections(conn, str(args.detect_id)):
            x, y = transformer.forward(intersection.lon, intersection.lat)
            circles.append({"id": intersection.intersection_id, "x": x, "y": y,
                            "radius_m": intersection.radius_m,
                            "tile": [math.floor(x / (args.pixel_size * args.tile_size)),
                                     math.floor(y / (args.pixel_size * args.tile_size))]})
    if int(pixels[:, 2].sum()) != source_count:
        raise ValueError("raster counts do not conserve source points")
    pixels = pixels[np.lexsort((pixels[:, 1], pixels[:, 0]))]
    tile_x = pixels[:, 0] // args.tile_size
    split_tile = (int(tile_x.min()) + int(tile_x.max())) // 2 if args.split_x is None else math.floor(
        args.split_x / (args.pixel_size * args.tile_size))
    roles = split_tiles(tile_x, split_tile)
    profile = fit_profile(pixels[roles == "train", 2], log_scale=args.log_scale,
                          asinh_scale=args.asinh_scale)
    metadata = {
        "version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "normalization preview; provisional spatial split, not a reviewed dataset",
        "database": config.database, "source_table": run.source_table,
        "detect_id": str(args.detect_id), "clean_id": run.clean_id,
        "clean_source_fingerprint": fingerprint,
        "source_scope": "all original rows of source_table, including points excluded by Clean",
        "source_points": source_count, "timestamp_range": [time_min, time_max],
        "raster_sha256": hashlib.sha256(pixels.astype("<i8").tobytes()).hexdigest(),
        "source_extent_4326": extent, "processing_srid": run.spatial_context.processing_srid,
        "projection_config": run.spatial_context.projection_config,
        "pixel_size_m": args.pixel_size, "tile_size": args.tile_size,
        "grid_origin_m": [0, 0], "pixel_assignment": "floor(x/res), floor(y/res); half-open bins; north-up PNG",
        "split_tile_x": split_tile,
        "split_rule": "train tx < split-1; preview tx >= split+1; remaining two columns buffer",
        "curve_parameters_status": "preview starting values, not tuned; see profile.json for actual scales",
        "rule_circles": circles,
        "split_stats": {role: {"occupied_pixels": int(np.count_nonzero(roles == role)),
                               "raw_points": int(pixels[roles == role, 2].sum())}
                        for role in ("train", "preview", "buffer")},
    }
    report = write_report(output, pixels, profile, metadata, circles)
    print(f"Report: {report}")
    print(f"Raw points: {source_count}; training P99.5: {profile['quantiles']['99.5']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
