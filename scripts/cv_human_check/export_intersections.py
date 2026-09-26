"""Export raw-point crops for every rule intersection centered inside a WGS84 bbox."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import uuid

import numpy as np
from PIL import Image

from track.v2.cli._load_config import load_startup_config
from track.v2.config import connect
from track.v2.intersections.query_results import fetch_detect_row, fetch_intersections
from track.v2.db.source_table import validate_source_table, verify_refine_source_fingerprint
from track.v2.projection import CoordinateTransformer

from .preview import fit_profile, normalize
from .tuning import png_data, render_comparison

METHODS = ("sqrt", "log1p", "asinh")


def crop_grid(x: float, y: float, radius: float, resolution: float) -> tuple[int, int, int, int]:
    """Outward-snap the circle's bounding rectangle to the shared pixel grid."""
    if not all(math.isfinite(v) for v in (x, y, radius, resolution)) or min(radius, resolution) <= 0:
        raise ValueError("finite coordinates and positive radius/resolution required")
    return (math.floor((x - radius) / resolution), math.floor((y - radius) / resolution),
            math.ceil((x + radius) / resolution), math.ceil((y + radius) / resolution))


def rasterize(pixels: np.ndarray, grid: tuple[int, int, int, int]) -> np.ndarray:
    ix0, iy0, ix1, iy1 = grid
    counts = np.zeros((iy1 - iy0, ix1 - ix0), dtype=np.int64)
    if pixels.size:
        if np.any((pixels[:, 0] < ix0) | (pixels[:, 0] >= ix1)
                  | (pixels[:, 1] < iy0) | (pixels[:, 1] >= iy1)):
            raise ValueError("pixel outside crop")
        np.add.at(counts, (iy1 - 1 - pixels[:, 1], pixels[:, 0] - ix0), pixels[:, 2])
    return counts


def unique_pixels(crops: list[dict]) -> np.ndarray:
    """Count overlapping crops once when estimating the shared intensity profile."""
    merged: dict[tuple[int, int], int] = {}
    for crop in crops:
        for ix, iy, count in crop["pixels"]:
            key = int(ix), int(iy)
            if key in merged and merged[key] != int(count):
                raise ValueError("overlapping pixel counts disagree")
            merged[key] = int(count)
    if not merged:
        raise ValueError("selected crops have no original trajectory points")
    return np.asarray([(ix, iy, count) for (ix, iy), count in sorted(merged.items())], dtype=np.int64)


def read_crop(conn, table: str, srid: int, grid: tuple[int, int, int, int],
              resolution: float, transformer: CoordinateTransformer) -> tuple[np.ndarray, list]:
    bounds = tuple(v * resolution for v in grid)
    # A conservative source envelope enables the GiST index; projected half-open
    # bounds then select exact pixels, including context outside the user's bbox.
    geographic = transformer.inverse_bounds(bounds)
    query = f"""
        WITH projected AS MATERIALIZED (
            SELECT ST_Transform(geom, %s) AS xy, timestamp
              FROM {table}
             WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
        )
        SELECT floor(ST_X(xy) / %s)::bigint, floor(ST_Y(xy) / %s)::bigint,
               count(*)::bigint, min(timestamp), max(timestamp)
          FROM projected
         WHERE ST_X(xy) >= %s AND ST_Y(xy) >= %s
           AND ST_X(xy) < %s AND ST_Y(xy) < %s
         GROUP BY 1, 2 ORDER BY 1, 2
    """
    with conn.cursor() as cur:
        cur.execute(query, (srid, *geographic, resolution, resolution, *bounds))
        rows = cur.fetchall()
    pixels = np.asarray([row[:3] for row in rows], dtype=np.int64).reshape(-1, 3)
    times = [value for row in rows for value in row[3:] if value is not None]
    return pixels, [min(times), max(times)] if times else [None, None]


def write_dataset(output: Path, crops: list[dict], metadata: dict, resolution: float) -> dict:
    pixels = unique_pixels(crops)
    profile = fit_profile(pixels[:, 2], log_scale=1.0, asinh_scale=1.0)
    profile["fit_scope"] = "unique occupied pixels in requested training candidate crops; overlaps counted once"
    profile["status"] = "initial parameters; no validation/test split or tuning performed"
    output.mkdir(parents=True, exist_ok=False)
    (output / "INCOMPLETE").write_text("Export in progress; require manifest.json and no INCOMPLETE marker.\n")
    for directory in ("counts", "labels", *(f"images/{method}" for method in METHODS)):
        (output / directory).mkdir(parents=True)
    samples, entries = [], []
    for crop in crops:
        info = {key: value for key, value in crop.items() if key != "pixels"}
        name = f"intersection-{info['intersection_id']}"
        counts = rasterize(crop["pixels"], tuple(info["grid_bounds"]))
        info.update({"width": counts.shape[1], "height": counts.shape[0],
                     "raw_points": int(counts.sum()), "occupied_pixels": int(np.count_nonzero(counts)),
                     "peak_count": int(counts.max()), "counts": f"counts/{name}.npz",
                     "label": f"labels/{name}.json", "images": {}, "mapping_stats": {}})
        np.savez_compressed(output / info["counts"], counts=counts)
        sample = {"id": name, "title": f"交叉口 {info['intersection_id']}",
                  "subtitle": f"{counts.shape[1]}×{counts.shape[0]} px · {info['raw_points']:,} 原始点 · r={info['radius_m']:.1f} m",
                  "width": counts.shape[1], "height": counts.shape[0],
                  "circles": [info["circle_pixels"]], "variants": []}
        for method in METHODS:
            gray = np.rint(255 * normalize(counts, profile, method, 99.5)).astype(np.uint8)
            relative = f"images/{method}/{name}.png"
            image = Image.fromarray(gray)
            image.save(output / relative)
            info["images"][method] = relative
            info["mapping_stats"][method] = {
                "quantized_to_zero": int(np.count_nonzero((counts > 0) & (gray == 0))),
                "clipped_pixels": int(np.count_nonzero(counts > profile["quantiles"]["99.5"])),
            }
            sample["variants"].append({"column": method, "when": {}, "image": png_data(image),
                                       "note": "P99.5" + (" · s=1（初始值）" if method != "sqrt" else "")})
        (output / info["label"]).write_text(json.dumps(info, ensure_ascii=False, indent=2, default=str))
        entries.append(info)
        samples.append(sample)
    manifest = {**metadata, "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "extracted_training_candidates_not_reviewed",
                "pixel_size_m": resolution, "grid_origin_m": [0, 0],
                "pixel_assignment": "floor(x/res), floor(y/res); half-open pixels; north-up images",
                "crop_policy": "outward-snapped circle bounding rectangle; no resize or circular masking",
                "input_policy": "original source points only; no lines, interpolation, denoising or label overlays",
                "normalization": {"profile": "profile.json", "percentile": 99.5, "methods": list(METHODS)},
                "split": "training_candidates_only; no validation/test split assigned",
                "label_status": "rule circle pseudo-labels; no human review or segmentation masks",
                "intersection_count": len(entries), "image_count": len(entries) * len(METHODS),
                "unique_occupied_pixels": len(pixels), "unique_raw_points": int(pixels[:, 2].sum()),
                "raw_points_with_crop_overlap": sum(item["raw_points"] for item in entries),
                "empty_intersection_ids": [item["intersection_id"] for item in entries if not item["raw_points"]],
                "unique_pixels_sha256": hashlib.sha256(pixels.astype("<i8").tobytes()).hexdigest(),
                "samples": entries}
    config = {"title": "交叉口训练候选 · 原始点灰度图", "axes": [],
              "description": ["仅绘制原始落点计数；圆仅供叠加查看，不进入训练图片。",
                              "统一 P99.5、log1p/asinh 尺度 s=1，为初始参数，尚未调优。",
                              "本批为所选交叉口训练候选，尚未人工复核、划分验证集或生成掩膜。"],
              "summary": [f"{len(entries)} 个交叉口", f"{resolution:g} 米/像素", f"{len(entries) * 3} 张灰度图"],
              "columns": [{"id": method, "title": method} for method in METHODS],
              "samples": samples, "metadata": {key: value for key, value in manifest.items() if key != "samples"}}
    (output / "index.html").write_text(render_comparison(config), encoding="utf-8")
    (output / "profile.json").write_text(json.dumps(profile, indent=2))
    (output / "README.md").write_text(
        "# 交叉口训练候选数据\n\n"
        "打开 `index.html` 比较全部交叉口的三组图片。\n\n"
        "- `images/{sqrt,log1p,asinh}/`：8 位灰度 PNG，北向朝上，不含规则圆绘制。\n"
        "- `counts/`：原始 int64 像素计数，可重新归一化，避免 PNG 量化损失。\n"
        "- `labels/`：来源交叉口 ID、经纬度、投影坐标、裁剪边界、像素圆与相邻规则圆。\n"
        "- `profile.json`：本批候选的共享标尺，重叠像素只统计一次；参数尚未调优。\n"
        "- `manifest.json`：数据库、Detect/Refine、范围、来源指纹与逐图统计。\n\n"
        "筛选以圆心落入指定经纬度范围（含边界）为准。裁剪保留完整圆的外接矩形，\n"
        "可包含筛选范围外的原始点；矩形角落不置零，不插值、不缩放。\n"
        "像素圆坐标以图像左上角边界为原点，像素中心为 (列+0.5, 行+0.5)。\n\n"
        "这是交叉口正样本候选，不是完整的整区域检测训练集。尚无人工复核、\n"
        "负样本、验证/测试划分或分割掩膜；相邻/重叠裁剪应归入同一空间分组。\n"
        "后续若从本批划出验证/测试区，需只用训练部分重新拟合 profile 并重生成图片。\n",
        encoding="utf-8")
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
    (output / "INCOMPLETE").unlink()
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", "-d", required=True)
    parser.add_argument("--detect-id", type=uuid.UUID, required=True)
    parser.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    parser.add_argument("--pixel-size", type=float, default=2.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    west, south, east, north = args.bbox
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        parser.error("bbox must be WEST SOUTH EAST NORTH in WGS84 degrees")
    if not math.isfinite(args.pixel_size) or args.pixel_size <= 0:
        parser.error("pixel size must be finite and positive")
    output = args.output.resolve()
    repository = Path(__file__).resolve().parents[2]
    data_root = Path(os.environ.get("DATA_ROOT", "/data")).resolve()
    if output == repository or repository in output.parents or data_root not in output.parents:
        parser.error("output must be a new directory under DATA_ROOT, outside the repository")
    if output.exists():
        parser.error("output exists; choose a new directory")
    pipeline_config = load_startup_config()
    with connect(replace(pipeline_config.postgres, database=args.database)) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '180s'")
        run = fetch_detect_row(conn, str(args.detect_id))
        if run.status != "completed":
            raise ValueError("export requires a completed Detect Run")
        intersections = fetch_intersections(conn, str(args.detect_id))
        selected = [i for i in intersections if west <= i.lon <= east and south <= i.lat <= north]
        if not selected:
            raise ValueError("no intersection centers within bbox")
        print(f"Selected {len(selected)}/{len(intersections)} intersections; verifying source snapshot...", flush=True)
        verify_refine_source_fingerprint(conn, run.refine_id)
        source = validate_source_table(conn, run.source_table)
        with conn.cursor() as cur:
            cur.execute("SELECT source_fingerprint FROM refine_runs WHERE id=%s", (run.refine_id,))
            fingerprint = cur.fetchone()[0]
            cur.execute(f"SELECT count(*), count(*) FILTER (WHERE geom IS NULL OR ST_IsEmpty(geom)) FROM {source.quoted_table}")
            source_count, invalid = cur.fetchone()
        if source_count != fingerprint["row_count"] or invalid:
            raise ValueError("source population differs from Refine snapshot or has missing geometry")
        transformer = CoordinateTransformer(run.spatial_context)
        projected = [(i, *transformer.forward(i.lon, i.lat)) for i in intersections]
        selected_ids = {i.intersection_id for i in selected}
        crops = []
        for intersection, x, y in projected:
            if intersection.intersection_id not in selected_ids:
                continue
            grid = crop_grid(x, y, intersection.radius_m, args.pixel_size)
            if (grid[2] - grid[0]) * (grid[3] - grid[1]) > 16_777_216:
                raise ValueError("crop exceeds 16 million pixels; increase --pixel-size")
            bounds = [v * args.pixel_size for v in grid]
            pixels, time_range = read_crop(conn, source.quoted_table, run.spatial_context.processing_srid,
                                           grid, args.pixel_size, transformer)
            nearby = []
            for other, ox, oy in projected:
                dx = max(bounds[0] - ox, 0.0, ox - bounds[2])
                dy = max(bounds[1] - oy, 0.0, oy - bounds[3])
                if dx * dx + dy * dy <= other.radius_m ** 2:
                    nearby.append({"intersection_id": other.intersection_id,
                                   "cx": (ox - bounds[0]) / args.pixel_size,
                                   "cy": (bounds[3] - oy) / args.pixel_size,
                                   "r": other.radius_m / args.pixel_size,
                                   "center_in_selection": other.intersection_id in selected_ids})
            crops.append({"intersection_id": intersection.intersection_id, "lon": intersection.lon,
                          "lat": intersection.lat, "radius_m": intersection.radius_m,
                          "center_xy_m": [x, y], "grid_bounds": list(grid), "bounds_m": bounds,
                          "geotransform": [bounds[0], args.pixel_size, 0, bounds[3], 0, -args.pixel_size],
                          "circle_pixels": {"cx": x / args.pixel_size - grid[0],
                                            "cy": grid[3] - y / args.pixel_size,
                                            "r": intersection.radius_m / args.pixel_size},
                          "nearby_rule_circles": nearby, "timestamp_range": time_range, "pixels": pixels})
            print(f"{len(crops)}/{len(selected)}: intersection {intersection.intersection_id}, {int(pixels[:, 2].sum())} points", flush=True)
        metadata = {"database": args.database, "source_table": run.source_table,
                    "detect_id": str(args.detect_id), "refine_id": run.refine_id,
                    "source_fingerprint": fingerprint, "source_fingerprint_verified": True,
                    "source_total_rows": source_count, "processing_srid": run.spatial_context.processing_srid,
                    "projection_config": run.spatial_context.projection_config,
                    "selection_bbox_wgs84": dict(zip(("west", "south", "east", "north"), args.bbox)),
                    "selection_rule": "intersection center within inclusive WGS84 bbox",
                    "total_detect_intersections": len(intersections)}
    manifest = write_dataset(output, crops, metadata, args.pixel_size)
    print(json.dumps({"output": str(output), "intersections": manifest["intersection_count"],
                      "images": manifest["image_count"], "unique_raw_points": manifest["unique_raw_points"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
