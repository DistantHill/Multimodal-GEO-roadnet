#!/usr/bin/env python3
"""Count points in selected grid AABBs, only count(*)."""

from __future__ import annotations
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from track.v2.cli._load_config import load_startup_config
from track.v2.db.source_table import validate_source_table
from track.v2.candidates.grid import create_grid
from track.v2.models import SpatialContext
from track.v2.projection import CoordinateTransformer

PROCESSING_SRID = 32649
GRID_SIZE_M = 5000.0
STATEMENT_TIMEOUT_MS = 60_000

# 与参考脚本保持一致
GRID_EXTENT = (
    298285.89554442314,
    3829533.5790700926,
    443285.89554442314,
    4024533.5790700926,
)

TARGETS = [
    (3, 6),
    (4, 7),
    (7, 0),
    (7, 1),
    (2, 0),
    (3, 1),
    (3, 6),
    (4, 7),
]

REFINE_ID = "6c14e329-04c7-45d1-87fe-8ab4ff7b767e"
PROCESSING_SRID = 32649
SOURCE_SRID = 4326
GRID_SIZE_M = 5000.0
STATEMENT_TIMEOUT_MS = 60_000

def main() -> int:
    os.environ["PIPELINE_CONFIG_PATH"] = str(
        Path(__file__).resolve().parents[1] / "config/pipeline-roadnet-test.toml"
    )

    config = load_startup_config()
    grids = create_grid(GRID_EXTENT, GRID_SIZE_M)
    spatial_context = SpatialContext(
        source_srid=SOURCE_SRID,
        processing_srid=PROCESSING_SRID,
        projection_config={
            "source_srid": SOURCE_SRID,
            "processing_srid": PROCESSING_SRID,
            "enforce_area_of_use": True,
            "max_scale_error": 0.01,
            "coordinate_semantics_version": 2,
        },
    )

    output_dir = Path(__file__).resolve().parents[1] / "generated/cross-track-point-counts"
    output_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []

    with psycopg.connect(config.postgres.dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_table, processing_srid FROM refine_runs WHERE id = %s",
                (REFINE_ID,),
            )
            metadata = cur.fetchone()
            if metadata is None:
                raise RuntimeError(f"unknown Refine Run: {REFINE_ID}")
            source_table, processing_srid = metadata
            if int(processing_srid) != PROCESSING_SRID:
                raise RuntimeError(
                    f"processing SRID mismatch: expected {PROCESSING_SRID}, "
                    f"got {processing_srid}"
                )
            source = validate_source_table(conn, str(source_table))
            transformer = CoordinateTransformer(spatial_context)
            print(f"source_table={source.source_table}")
            print(f"quoted_source_table={source.quoted_table}")

            cur.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(STATEMENT_TIMEOUT_MS),),
            )

            for ordinal, (col, row) in enumerate(TARGETS, start=1):
                aabb = (
                    float(grids[col][row][0]),
                    float(grids[col][row][1]),
                    float(grids[col][row][2]),
                    float(grids[col][row][3]),
                )
                xmin, ymin, xmax, ymax = aabb
                lon_min, lat_min, lon_max, lat_max = transformer.inverse_bounds(aabb)
                projected = (
                    f"ST_Transform(s.geom, {PROCESSING_SRID})"
                )
                sql = f"""
                    SELECT count(*) AS points_count
                    FROM {source.quoted_table} AS s
                    WHERE s.geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                      AND ST_X({projected}) >= %s
                      AND ST_Y({projected}) >= %s
                      AND ST_X({projected}) <= %s
                      AND ST_Y({projected}) <= %s
                """
                params = (
                    lon_min, lat_min, lon_max, lat_max,
                    xmin, ymin, xmax, ymax,
                )
                execute: Any = cur.execute
                execute(sql, params)
                result = cur.fetchone()
                if result is None:
                    raise RuntimeError("count query returned no row")
                count = result[0]

                print(
                    f"[{ordinal}/{len(TARGETS)}] "
                    f"grid=({col}, {row}) "
                    f"AABB={aabb} "
                    f"points_count={count}"
                )

                rows.append(
                    {
                        "ordinal": ordinal,
                        "grid_index": [col, row],
                        "aabb": list(aabb),
                        "points_count": count,
                    }
                )

    json_path = output_dir / "points_counts.json"
    json_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    csv_path = output_dir / "points_counts.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "ordinal",
                "col",
                "row",
                "xmin",
                "ymin",
                "xmax",
                "ymax",
                "points_count",
            ],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(
                {
                    "ordinal": r["ordinal"],
                    "col": r["grid_index"][0],
                    "row": r["grid_index"][1],
                    "xmin": r["aabb"][0],
                    "ymin": r["aabb"][1],
                    "xmax": r["aabb"][2],
                    "ymax": r["aabb"][3],
                    "points_count": r["points_count"],
                }
            )

    print(f"json={json_path}")
    print(f"csv={csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
