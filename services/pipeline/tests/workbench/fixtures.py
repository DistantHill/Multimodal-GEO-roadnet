"""Deterministic source generator; all generated datasets stay under pytest tmp.

Three genuine four-arm intersections share a main road. Multiple independent
vehicles traverse both directions and turn at every junction. A separate long
straight road is a hard negative, not merely an empty geographic box.
"""
from __future__ import annotations

import copy
import csv
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import random

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from pyogrio.raw import write
from shapely.geometry import LineString, Point, box

from pipeline.config import DEFAULTS


def _sample(vertices: list[tuple[float, float]], spacing: float = 20.0) -> list[tuple[float, float]]:
    points = [vertices[0]]
    for a, b in zip(vertices, vertices[1:]):
        distance = math.dist(a, b)
        steps = max(1, math.ceil(distance / spacing))
        points.extend((a[0] + (b[0] - a[0]) * step / steps,
                       a[1] + (b[1] - a[1]) * step / steps) for step in range(1, steps + 1))
    return points


def _write_vector(path: Path, layer: str, geometries: list, fields: dict) -> None:
    write(path, np.array([geometry.wkb for geometry in geometries], dtype=object),
          [np.array(values) for values in fields.values()], list(fields),
          layer=layer, geometry_type=geometries[0].geom_type, crs="EPSG:32650")


def make_dataset(root: Path, *, replicas: int = 4) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    northing = 4_400_000.0
    junctions = [(500_000.0 + i * 1500.0, northing) for i in range(3)]
    west, east = junctions[0][0] - 300.0, junctions[-1][0] + 300.0
    rows, vehicle_number = [], 0
    epoch = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def add_track(vertices: list[tuple[float, float]], kind: str, replica: int) -> None:
        nonlocal vehicle_number
        vehicle_number += 1
        coordinates = _sample(vertices)
        begin = epoch + timedelta(seconds=vehicle_number * 1000)
        elapsed = 0.0
        for i, (x, y) in enumerate(coordinates):
            if i:
                elapsed += math.dist(coordinates[i - 1], coordinates[i]) / 5.0
            a, b = (coordinates[i], coordinates[i + 1]) if i + 1 < len(coordinates) else (coordinates[i - 1], coordinates[i])
            heading = math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360
            rows.append({"id": len(rows) + 1, "vehicle_id": f"vehicle-{vehicle_number:04d}",
                         "timestamp": (begin + timedelta(seconds=elapsed)).isoformat(),
                         "x": x, "y": y, "heading": heading, "speed": 5.0,
                         "fleet": "synthetic", "payload_kg": 1200 + replica,
                         "source_note": kind})

    for replica in range(replicas):
        offset = (replica - (replicas - 1) / 2.0) * 0.4
        for reverse in (False, True):
            main = [(west, northing + offset), (east, northing + offset)]
            add_track(list(reversed(main)) if reverse else main, "connected-main-road", replica)
            negative = [(west, northing + 500 + offset), (east, northing + 500 + offset)]
            add_track(list(reversed(negative)) if reverse else negative, "straight-negative-road", replica)
            for x, y in junctions:
                crossing = [(x + offset, y - 300), (x + offset, y + 300)]
                add_track(list(reversed(crossing)) if reverse else crossing, "crossing-road", replica)
                turning = [(x - 120, y + offset), (x + offset, y + offset), (x + offset, y + 120)]
                add_track(list(reversed(turning)) if reverse else turning, "turning-track", replica)
    # Input order must not determine subtracks or native point provenance.
    random.Random(729).shuffle(rows)
    csv_path, parquet_path = root / "trajectories.csv", root / "trajectories.parquet"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    pq.write_table(pa.Table.from_pylist(rows), parquet_path, compression="zstd", row_group_size=256)

    labels_path = root / "labels.gpkg"
    _write_vector(labels_path, "intersections", [Point(x, y) for x, y in junctions],
                  {"label_id": ["junction-a", "junction-b", "junction-c"],
                   "type": ["cross"] * 3, "reviewer": ["synthetic-ground-truth"] * 3})
    regions = [box(x - 180, y - 180, x + 180, y + 180) for x, y in junctions]
    regions.append(box(junctions[1][0] - 180, northing + 400,
                       junctions[1][0] + 180, northing + 600))
    _write_vector(labels_path, "regions", regions,
                  {"region_id": ["junction-a", "junction-b", "junction-c", "negative-road"],
                   "split": ["tune", "tune", "holdout", "tune"], "reviewed": [1, 1, 1, 1]})
    lines = [LineString([(west, northing), (east, northing)]),
             LineString([(west, northing + 500), (east, northing + 500)])]
    lines.extend(LineString([(x, y - 300), (x, y + 300)]) for x, y in junctions)
    _write_vector(labels_path, "centerlines", lines,
                  {"label_id": ["main", "negative", "cross-a", "cross-b", "cross-c"]})
    return {"csv": csv_path, "parquet": parquet_path, "labels": labels_path,
            "row_count": len(rows), "vehicle_count": vehicle_number, "junctions": junctions,
            "ground_truth_centerlines": lines}


def make_config(dataset: dict, output_root: Path, *, kind: str = "parquet") -> dict:
    config = copy.deepcopy(DEFAULTS)
    config["input"].update(kind=kind, path=str(dataset[kind]), dataset_id="synthetic-roadnet",
                           source_crs="EPSG:32650", processing_crs="EPSG:32650",
                           timestamp_unit="iso", speed_unit="m/s", timezone="UTC")
    config["input"]["columns"].update(x="x", y="y")
    config["output"]["root"] = str(output_root)
    config["database"]["enabled"] = False
    config["resources"].update(threads=1, batch_rows=257, sample_interval_ms=10,
                               duckdb_memory_mb=128, soft_memory_mb=2048)
    config["labels"] = {
        "intersections": {"path": str(dataset["labels"]), "layer": "intersections", "id_field": "label_id"},
        "centerlines": {"path": str(dataset["labels"]), "layer": "centerlines", "id_field": "label_id"},
        "regions": {"path": str(dataset["labels"]), "layer": "regions", "id_field": "region_id",
                    "split_field": "split", "reviewed_field": "reviewed"},
    }
    config["tuning"].update(n_trials=3, startup_trials=1, warmup_steps=0,
                            study_name="synthetic-integration", objective="intersection_f1",
                            matching_radius_m=40.0,
                            search_space={"detect.cluster_eps_m": {"type": "float", "low": 15.0, "high": 40.0}})
    return config
