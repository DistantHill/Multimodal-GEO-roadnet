"""Bounded spatial orchestration around native turn/cross/cluster/topology kernels."""
from __future__ import annotations

from collections import defaultdict
import math
from pathlib import Path

import numpy as np
import pyarrow as pa
from shapely.geometry import Point, mapping

from . import _native
from .artifacts import (EVENT_FIELDS, SEGMENT_FIELDS, ParquetSink, key, schema,
                        seal_stage, write_features, write_geojson)
from .resources import ResourceLimitExceeded
from .source import connection, iter_groups


def _array(rows: list[dict], fields: tuple[str, ...]) -> np.ndarray:
    return np.asarray([[row[name] if row[name] is not None else np.nan for name in fields]
                       for row in rows], dtype=np.float64).reshape(-1, len(fields))


def _bounded_rows(reader, limit: int) -> list[dict]:
    rows: list[dict] = []
    for batch in reader:
        rows.extend(batch.to_pylist())
        if len(rows) > limit:
            raise ResourceLimitExceeded(f"spatial tile exceeds resources.max_tile_segments={limit}")
    return rows


def run_detect(config: dict, run_id: str, directory: Path, refine_dir: Path,
               monitor) -> tuple[dict, list[dict]]:
    directory.mkdir(parents=True, exist_ok=True)
    cfg, resources = config["detect"], config["resources"]
    native_cfg = {**cfg, **{name: resources[name] for name in ("max_candidates", "max_pair_checks", "max_grid_memberships")}}
    crs = config["input"]["processing_crs"]
    refine_id, detect_id = key(run_id, "refine"), key(run_id, "detect")
    points_path, segments_path = refine_dir / "points.parquet", refine_dir / "segments.parquet"
    tiles_path = directory / "segment_tiles.parquet"
    tile_size = cfg["tile_size_m"]
    candidates: list[dict] = []

    def add_candidate(kind: str, x: float, y: float, angle: float, *, point_id=None, segment_a=None, segment_b=None):
        if len(candidates) >= resources["max_candidates"]:
            raise ResourceLimitExceeded("Detect exceeded resources.max_candidates")
        candidates.append({"event_id": key(detect_id, kind, point_id, segment_a, segment_b),
                           "run_id": run_id, "refine_id": refine_id, "detect_id": detect_id,
                           "kind": kind, "x": float(x), "y": float(y), "angle_deg": float(angle),
                           "point_id": point_id, "segment_a_id": segment_a, "segment_b_id": segment_b,
                           "intersection_id": None, "geometry": Point(x, y).wkb})

    with monitor.stage("detect") as span, connection(config, directory / "spill") as database:
        # Turning candidates keep full track context, irrespective of Arrow batch boundaries.
        groups = iter_groups(database, "SELECT * FROM read_parquet(?) ORDER BY vehicle_id, subtrack_index, sequence",
                             [str(points_path)], ("vehicle_id", "subtrack_id"), resources["batch_rows"], resources["max_vehicle_points"])
        while True:
            with span.phase("read"):
                rows = next(groups, None)
            if rows is None:
                break
            with span.phase("compute"):
                turns = _native.turns(_array(rows, ("x", "y")), _array(rows, ("timestamp",)).ravel(),
                                      _array(rows, ("heading_cal",)).ravel(),
                                      np.asarray([r["subtrack_index"] for r in rows], dtype=np.int64),
                                      np.asarray([r["sequence"] for r in rows], dtype=np.int64), cfg)
            for position, index, angle in zip(turns["xy"], turns["index"], turns["turn_deg"], strict=True):
                add_candidate("turn", *position, angle, point_id=rows[int(index)]["point_id"])
            span.add_counts(turn_candidates=len(turns["index"]))

        # Each whole segment is present in every tile its bbox touches. The
        # exact crossing's half-open owner tile emits it once, including borders.
        with span.phase("read"):
            membership_count = database.execute(
                "SELECT coalesce(sum((floor(max_x/?)-floor(min_x/?)+1)*(floor(max_y/?)-floor(min_y/?)+1)),0), count(*) FROM read_parquet(?) WHERE length_m <= ?",
                [tile_size] * 4 + [str(segments_path), cfg["max_segment_length_m"]]).fetchall()[0]
            rejected_long = database.execute("SELECT count(*) FROM read_parquet(?) WHERE length_m > ?",
                                             [str(segments_path), cfg["max_segment_length_m"]]).fetchall()[0][0]
        if membership_count[0] > resources["max_grid_memberships"]:
            raise ResourceLimitExceeded("Segment tile memberships exceed resources.max_grid_memberships")
        tile_schema = schema(SEGMENT_FIELDS + [("tile_x", pa.int64()), ("tile_y", pa.int64())], crs, "LineString")
        with span.phase("write"), ParquetSink(tiles_path, tile_schema, resources["batch_rows"]) as sink:
            reader = database.sql(
                "SELECT s.*, gx.tile_x, gy.tile_y FROM read_parquet(?) s, range(CAST(floor(min_x/?) AS BIGINT),CAST(floor(max_x/?) AS BIGINT)+1) gx(tile_x), range(CAST(floor(min_y/?) AS BIGINT),CAST(floor(max_y/?) AS BIGINT)+1) gy(tile_y) WHERE length_m<=? ORDER BY tile_x,tile_y,segment_order",
                params=[str(segments_path)] + [tile_size] * 4 + [cfg["max_segment_length_m"]]).to_arrow_reader(resources["batch_rows"])
            for batch in reader:
                monitor.check()
                sink.extend(batch.to_pylist())
        groups = iter_groups(database, "SELECT * FROM read_parquet(?) ORDER BY tile_x,tile_y,segment_order", [str(tiles_path)],
                             ("tile_x", "tile_y"), resources["batch_rows"], resources["max_tile_segments"])
        while True:
            with span.phase("read"):
                rows = next(groups, None)
            if rows is None:
                break
            with span.phase("compute"):
                result = _native.crossings(_array(rows, ("x1", "y1", "x2", "y2")),
                    np.asarray([r["vehicle_code"] for r in rows], dtype=np.int64), native_cfg,
                    _array(rows, ("heading1", "heading2")), _array(rows, ("speed1", "speed2")))
            owner = rows[0]["tile_x"], rows[0]["tile_y"]
            for position, a, b, angle in zip(result["xy"], result["segment_a"], result["segment_b"], result["angle_deg"], strict=True):
                if (math.floor(position[0] / tile_size), math.floor(position[1] / tile_size)) != owner:
                    continue
                identifiers = sorted((rows[int(a)]["segment_id"], rows[int(b)]["segment_id"]))
                add_candidate("cross", *position, angle, segment_a=identifiers[0], segment_b=identifiers[1])
            span.add_counts(tiles=1)
        span.add_counts(input_segments=int(membership_count[1]), segment_tile_memberships=int(membership_count[0]),
                        skipped_long_segments=int(rejected_long), candidate_events=len(candidates))
        with span.phase("compute"):
            clustered = _native.cluster(_array(candidates, ("x", "y")), cfg["cluster_eps_m"],
                                        cfg["cluster_min_samples"], resources["max_pair_checks"])
        centers = clustered["centers"]
        per_tile: dict[tuple[int, int], list[int]] = defaultdict(list)
        for index, center in enumerate(centers):
            per_tile[(math.floor(center[0] / tile_size), math.floor(center[1] / tile_size))].append(index)
        accepted: dict[int, dict] = {}
        for (tx, ty), indices in sorted(per_tile.items()):
            radius = cfg["topology_radius_m"]
            x0, y0, x1, y1 = tx * tile_size - radius, ty * tile_size - radius, (tx + 1) * tile_size + radius, (ty + 1) * tile_size + radius
            with span.phase("read"):
                reader = database.sql(
                    "SELECT DISTINCT * EXCLUDE(tile_x,tile_y) FROM read_parquet(?) WHERE tile_x BETWEEN ? AND ? AND tile_y BETWEEN ? AND ? AND max_x>=? AND min_x<=? AND max_y>=? AND min_y<=? ORDER BY segment_order",
                    params=[str(tiles_path), math.floor(x0 / tile_size), math.floor(x1 / tile_size), math.floor(y0 / tile_size), math.floor(y1 / tile_size), x0, x1, y0, y1]).to_arrow_reader(resources["batch_rows"])
                rows = _bounded_rows(reader, resources["max_tile_segments"])
            with span.phase("compute"):
                topology = _native.topology(np.ascontiguousarray(centers[indices]),
                    _array(rows, ("x1", "y1", "x2", "y2")), np.asarray([r["vehicle_code"] for r in rows], dtype=np.int64), native_cfg)
            for local, index in enumerate(indices):
                if not bool(topology["valid"][local]):
                    continue
                point = Point(centers[index])
                identifier = key(detect_id, "intersection", index)
                arm_lo, arm_hi = int(topology["arm_offsets"][local]), int(topology["arm_offsets"][local + 1])
                properties = {"intersection_id": identifier, "run_id": run_id, "refine_id": refine_id,
                              "detect_id": detect_id, "radius_m": radius,
                              "candidate_count": int(clustered["counts"][index]),
                              "arm_count": int(topology["arm_count"][local]),
                              "vehicle_count": int(topology["vehicle_count"][local]),
                              "arm_bearings": topology["arm_bearings"][arm_lo:arm_hi].tolist()}
                accepted[index] = {"type": "Feature", "id": identifier, "properties": properties, "geometry": mapping(point)}
        features = [accepted[index] for index in sorted(accepted)]
        for candidate, label in zip(candidates, clustered["labels"], strict=True):
            if int(label) in accepted:
                candidate["intersection_id"] = accepted[int(label)]["id"]
        with span.phase("write"):
            with ParquetSink(directory / "candidates.parquet", schema(EVENT_FIELDS, crs, "Point")) as sink:
                sink.extend(candidates)
            write_features(directory / "intersections.parquet", features, crs, "Point")
            if config["output"]["geojson"]:
                write_geojson(directory / "intersections.geojson", features, crs)
        span.add_counts(intersections=len(features))
        with span.phase("write"):
            manifest = seal_stage(directory, "detect", detect_id, {"refine_id": refine_id},
                         {"intersections": (directory / "intersections.parquet", "id"),
                          "candidates": (directory / "candidates.parquet", "event_id"),
                          "segment_tiles": (tiles_path, ["segment_id", "tile_x", "tile_y"])}, cfg)
    return manifest, features
