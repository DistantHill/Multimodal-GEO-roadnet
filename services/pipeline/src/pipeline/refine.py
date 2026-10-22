"""Refine orchestration; every trajectory decision is made by the C++ kernel."""
from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
from pyproj import Transformer
from shapely.geometry import LineString, Point

from . import _native
from .artifacts import (POINT_FIELDS, SEGMENT_FIELDS, SOURCE_FIELDS, ParquetSink,
                        key, schema, seal_stage)
from .source import PreparedSource, connection, iter_groups


def run_refine(config: dict, run_id: str, directory: Path, source: PreparedSource,
               region, monitor) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    stage_id = key(run_id, "refine")
    crs = config["input"]["processing_crs"]
    to_wgs84 = Transformer.from_crs(crs, 4326, always_xy=True)
    resource = config["resources"]
    fields = SOURCE_FIELDS + [("run_id", pa.string()), ("refine_id", pa.string()), ("reason", pa.string())]
    query = "SELECT * FROM read_parquet(?)"
    parameters: list = [str(source.path)]
    if region is not None:
        halo = config["tuning"]["context_halo_m"]
        x0, y0, x1, y1 = region.bounds
        query += " WHERE x >= ? AND y >= ? AND x <= ? AND y <= ?"
        parameters.extend([x0 - halo, y0 - halo, x1 + halo, y1 + halo])
    query += " ORDER BY vehicle_id, source_seq"
    with monitor.stage("refine") as span, ExitStack() as stack:
        database = stack.enter_context(connection(config, directory / "spill"))
        points = stack.enter_context(ParquetSink(directory / "points.parquet", schema(POINT_FIELDS, crs, "Point"), resource["batch_rows"]))
        excluded = stack.enter_context(ParquetSink(directory / "excluded.parquet", schema(fields, crs, "Point")))
        segments = stack.enter_context(ParquetSink(directory / "segments.parquet", schema(SEGMENT_FIELDS, crs, "LineString"), resource["batch_rows"]))
        groups = iter_groups(database, query, parameters, ("vehicle_id",), resource["batch_rows"], resource["max_vehicle_points"])
        segment_order = 0
        while True:
            with span.phase("read"):
                original = next(groups, None)
            if original is None:
                break
            span.add_counts(input_rows=len(original))
            # A spatially filtered input may have missing source sequence ranges.
            # Treat them as I/O boundaries, never bridge an unobserved excursion.
            splits = [0] + [i for i in range(1, len(original)) if original[i]["source_seq"] != original[i - 1]["source_seq"] + 1] + [len(original)]
            offset = 0
            for lo, hi in zip(splits[:-1], splits[1:], strict=True):
                rows = original[lo:hi]
                xy = np.asarray([(row["x"], row["y"]) for row in rows], dtype=np.float64)
                values = lambda name: np.asarray([row[name] if row[name] is not None else np.nan for row in rows], dtype=np.float64)
                with span.phase("compute"):
                    result = _native.refine(xy, values("timestamp"), values("heading"), values("speed"), config["refine"])
                produced = []
                longitudes, latitudes = to_wgs84.transform(result["xy"][:, 0], result["xy"][:, 1])
                with span.phase("write"):
                    for i, position in enumerate(result["xy"]):
                        parent_index = int(result["source_index"][i])
                        before, after = int(result["from_index"][i]), int(result["to_index"][i])
                        base = dict(rows[parent_index if parent_index >= 0 else before])
                        subtrack_index = offset + int(result["subtrack_index"][i])
                        subtrack = key(stage_id, base["vehicle_id"], subtrack_index)
                        sequence = int(result["sequence"][i])
                        point_id = key(subtrack, sequence)
                        if parent_index < 0:
                            base.update(source_id=None, source_point_id=None,
                                        attributes_json=json.dumps({"producer": "orthogonal_injection",
                                            "from_attributes": json.loads(rows[before]["attributes_json"]),
                                            "to_attributes": json.loads(rows[after]["attributes_json"])}, ensure_ascii=False, allow_nan=False))
                        item = {**base, "run_id": run_id, "refine_id": stage_id, "point_id": point_id,
                                "subtrack_id": subtrack, "subtrack_index": subtrack_index,
                                "sequence": sequence, "x": float(position[0]), "y": float(position[1]),
                                "lon": float(longitudes[i]), "lat": float(latitudes[i]),
                                "timestamp": float(result["timestamp"][i]),
                                "heading": float(result["heading"][i]), "heading_cal": float(result["heading_cal"][i]),
                                "speed": float(result["speed"][i]),
                                "kind": "injected" if parent_index < 0 else "kept",
                                "from_source_point_id": rows[before]["source_point_id"],
                                "to_source_point_id": rows[after]["source_point_id"],
                                "ratio_in_gap": float(result["ratio_in_gap"][i]),
                                "geometry": Point(position).wkb}
                        points.append(item)
                        produced.append(item)
                    for index, reason in zip(result["excluded_index"], result["excluded_reason"], strict=True):
                        excluded.append({**rows[int(index)], "run_id": run_id, "refine_id": stage_id, "reason": reason})
                    for before, after in zip(produced[:-1], produced[1:], strict=True):
                        if before["subtrack_id"] != after["subtrack_id"]:
                            continue
                        geometry = LineString([(before["x"], before["y"]), (after["x"], after["y"])])
                        if geometry.length <= 1e-9:
                            continue
                        segments.append({"segment_id": key(stage_id, before["point_id"], after["point_id"]),
                            "segment_order": segment_order,
                            "run_id": run_id, "refine_id": stage_id, "vehicle_id": before["vehicle_id"],
                            "vehicle_code": before["vehicle_code"], "subtrack_id": before["subtrack_id"],
                            "from_point_id": before["point_id"], "to_point_id": after["point_id"],
                            "x1": before["x"], "y1": before["y"], "x2": after["x"], "y2": after["y"],
                            "min_x": geometry.bounds[0], "min_y": geometry.bounds[1], "max_x": geometry.bounds[2], "max_y": geometry.bounds[3],
                            "t1": before["timestamp"], "t2": after["timestamp"],
                            "heading1": before["heading_cal"], "heading2": after["heading_cal"],
                            "speed1": before["speed"], "speed2": after["speed"],
                            "length_m": geometry.length, "geometry": geometry.wkb})
                        segment_order += 1
                    if len(result["subtrack_index"]):
                        offset += int(np.max(result["subtrack_index"]))
                span.add_counts(output_rows=len(produced), excluded_rows=len(result["excluded_index"]),
                                injected_rows=int(np.count_nonzero(result["source_index"] < 0)))
        # Flush inside the measured write phase; close+rename occurs at stage exit.
        with span.phase("write"):
            points.close()
            segments.close()
            excluded.close()
            manifest = seal_stage(directory, "refine", stage_id, {"source_sha256": source.identity["sha256"]},
                      {"points": (directory / "points.parquet", "point_id"),
                       "excluded": (directory / "excluded.parquet", "source_point_id"),
                       "segments": (directory / "segments.parquet", "segment_id")}, config["refine"])
    return manifest
