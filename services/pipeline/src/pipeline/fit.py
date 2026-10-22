"""Stream connected-pair clip groups into native arclength/median fitting."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
from shapely.geometry import LineString, mapping

from . import _native
from .artifacts import ParquetSink, key, schema, seal_stage, write_features, write_geojson
from .source import connection, iter_groups


def run_fit(config: dict, run_id: str, directory: Path, clip_dir: Path,
            monitor) -> tuple[dict, list[dict]]:
    directory.mkdir(parents=True, exist_ok=True)
    cfg, resources = config["fit"], config["resources"]
    fit_id, clip_stage_id = key(run_id, "fit"), key(run_id, "clip")
    crs = config["input"]["processing_crs"]
    fields = [(name, pa.string()) for name in ("centerline_id", "clip_id", "run_id", "fit_id", "clip_stage_id", "geometry_source")]
    # Memberships carry the clip geometry and support attributes for independent
    # visual inspection; resolving clip_id is optional, never required to draw.
    fields += [("vehicle_id", pa.string()), ("reverse", pa.bool_()), ("length_m", pa.float64()), ("geometry", pa.binary())]
    features = []
    with monitor.stage("fit") as span, connection(config, directory / "spill") as database, ParquetSink(directory / "memberships.parquet", schema(fields, crs, "LineString")) as memberships:
        groups = iter_groups(database,
            "SELECT * FROM read_parquet(?) WHERE clip_type='between_intersection' ORDER BY pair_a,pair_b,clip_id",
            [str(clip_dir / "clips.parquet")], ("pair_a", "pair_b"), resources["batch_rows"],
            resources["max_fit_points"], weight_field="xy")
        while True:
            with span.phase("read"):
                group = next(groups, None)
            if group is None:
                break
            span.add_counts(input_clips=len(group))
            choices = [("combined", group)]
            if cfg["emit_directional"]:
                choices += [("a_to_b", [row for row in group if not row["reverse"]]),
                            ("b_to_a", [row for row in group if row["reverse"]])]
            for variant, rows in choices:
                if not rows:
                    continue
                xy = np.asarray([point for row in rows for point in row["xy"]], dtype=np.float64).reshape(-1, 2)
                offsets = np.concatenate(([0], np.cumsum([len(row["xy"]) for row in rows], dtype=np.int64))).astype(np.int64)
                with span.phase("compute"):
                    result = _native.fit(xy, offsets, np.asarray([row["reverse"] for row in rows], dtype=np.uint8),
                                         {**cfg, "max_fit_points": resources["max_fit_points"]})
                if result["status"] != "ok":
                    span.add_counts(skipped_variants=1)
                    continue
                start, end = rows[0]["pair_a"], rows[0]["pair_b"]
                coordinates = result["xy"]
                if variant == "b_to_a":
                    start, end = end, start
                    coordinates = coordinates[::-1]
                centerline_id = key(fit_id, rows[0]["pair_a"], rows[0]["pair_b"], variant)
                source_rows = [rows[int(index)] for index in result["used_clip_index"]]
                properties = {"centerline_id": centerline_id, "run_id": run_id,
                              "refine_id": key(run_id, "refine"), "detect_id": key(run_id, "detect"),
                              "clip_stage_id": clip_stage_id, "fit_id": fit_id,
                              "from_intersection_id": start, "to_intersection_id": end,
                              "variant": variant, "length_m": float(result["length_m"]),
                              "source_clip_count": int(result["clip_count"]),
                              "forward_clip_count": int(result["reverse_clip_count"] if variant == "b_to_a" else result["forward_clip_count"]),
                              "reverse_clip_count": int(result["forward_clip_count"] if variant == "b_to_a" else result["reverse_clip_count"]),
                              "source_clip_ids": [row["clip_id"] for row in source_rows]}
                features.append({"type": "Feature", "id": centerline_id,
                                 "geometry": mapping(LineString(coordinates)), "properties": properties})
                with span.phase("write"):
                    for row in source_rows:
                        memberships.append({"centerline_id": centerline_id, "clip_id": row["clip_id"],
                            "run_id": run_id, "fit_id": fit_id, "clip_stage_id": clip_stage_id,
                            "geometry_source": "clip", "vehicle_id": row["vehicle_id"],
                            "reverse": row["reverse"], "length_m": row["length_m"], "geometry": row["geometry"]})
            monitor.check()
        with span.phase("write"):
            memberships.close()
            write_features(directory / "centerlines.parquet", features, crs, "LineString")
            if config["output"]["geojson"]:
                write_geojson(directory / "centerlines.geojson", features, crs)
        span.add_counts(centerlines=len(features))
        with span.phase("write"):
            manifest = seal_stage(directory, "fit", fit_id, {"clip_stage_id": clip_stage_id, "detect_id": key(run_id, "detect")},
                          {"centerlines": (directory / "centerlines.parquet", "id"),
                           "memberships": (directory / "memberships.parquet", ["centerline_id", "clip_id"])}, cfg)
    return manifest, features
