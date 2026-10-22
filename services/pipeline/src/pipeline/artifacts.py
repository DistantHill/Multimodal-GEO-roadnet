"""Self-contained, atomically published stage artifacts and primary keys."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.parquet as pq
from pyproj import CRS, Transformer
from shapely import transform
from shapely.geometry import mapping, shape


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def key(*parts: Any) -> str:
    return digest(parts)[:32]


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".partial",
                                         delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(value, ensure_ascii=False, indent=2,
                                    sort_keys=True, allow_nan=False) + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


S = pa.string()
F = pa.float64()
I = pa.int64()
B = pa.binary()

SOURCE_FIELDS = [
    ("source_point_id", S), ("source_id", S), ("vehicle_id", S), ("vehicle_code", I),
    ("source_seq", I), ("timestamp", F), ("x", F), ("y", F), ("lon", F), ("lat", F),
    ("heading", F), ("speed", F), ("attributes_json", S), ("geometry", B),
]
POINT_FIELDS = SOURCE_FIELDS + [
    ("run_id", S), ("refine_id", S), ("point_id", S), ("subtrack_id", S),
    ("subtrack_index", I), ("sequence", I), ("kind", S), ("heading_cal", F),
    ("from_source_point_id", S), ("to_source_point_id", S), ("ratio_in_gap", F),
]
SEGMENT_FIELDS = [
    ("segment_id", S), ("segment_order", I), ("run_id", S), ("refine_id", S), ("vehicle_id", S),
    ("vehicle_code", I), ("subtrack_id", S), ("from_point_id", S), ("to_point_id", S),
    ("x1", F), ("y1", F), ("x2", F), ("y2", F), ("min_x", F), ("min_y", F),
    ("max_x", F), ("max_y", F), ("t1", F), ("t2", F), ("heading1", F),
    ("heading2", F), ("speed1", F), ("speed2", F), ("length_m", F), ("geometry", B),
]
EVENT_FIELDS = [
    ("event_id", S), ("run_id", S), ("refine_id", S), ("detect_id", S), ("kind", S),
    ("x", F), ("y", F), ("angle_deg", F), ("point_id", S), ("segment_a_id", S),
    ("segment_b_id", S), ("intersection_id", S), ("geometry", B),
]
CLIP_FIELDS = [
    ("clip_id", S), ("run_id", S), ("refine_id", S), ("detect_id", S), ("clip_stage_id", S),
    ("vehicle_id", S), ("subtrack_id", S), ("from_intersection_id", S),
    ("to_intersection_id", S), ("pair_a", S), ("pair_b", S), ("reverse", pa.bool_()),
    ("clip_type", S), ("point_ids", pa.list_(S)), ("point_attributes_json", pa.list_(S)),
    ("xy", pa.list_(pa.list_(F, 2))), ("length_m", F), ("geometry", B),
]


def schema(fields: list, crs: str, geometry_type: str) -> pa.Schema:
    geo = {"version": "1.1.0", "primary_column": "geometry", "columns": {
        "geometry": {"encoding": "WKB", "geometry_types": [geometry_type],
                     "crs": CRS(crs).to_json_dict()}}}
    metadata = {b"roadnet_schema_version": b"1"}
    if any(item[0] == "geometry" for item in fields):
        metadata[b"geo"] = json.dumps(geo).encode()
    return pa.schema(fields).with_metadata(metadata)


class ParquetSink:
    """An empty output remains a readable typed dataset; errors never publish it."""

    def __init__(self, path: Path, arrow_schema: pa.Schema, batch_rows: int = 65536):
        self.path, self.schema, self.batch_rows = path, arrow_schema, batch_rows
        path.parent.mkdir(parents=True, exist_ok=True)
        self.temporary = path.with_name(path.name + ".partial")
        self.writer = pq.ParquetWriter(self.temporary, arrow_schema, compression="zstd")
        self.pending: list[dict] = []
        self.count = 0
        self.closed = False

    def append(self, row: dict) -> None:
        self.pending.append(row)
        if len(self.pending) >= self.batch_rows:
            self.flush()

    def extend(self, rows: Iterable[dict]) -> None:
        for row in rows:
            self.append(row)

    def flush(self) -> None:
        if self.pending:
            table = pa.Table.from_pylist(self.pending, schema=self.schema)
            self.writer.write_table(table, row_group_size=self.batch_rows)
            self.count += len(self.pending)
            self.pending.clear()

    def close(self, *, publish: bool = True) -> None:
        if self.closed:
            return
        try:
            try:
                if publish:
                    self.flush()
            finally:
                self.writer.close()
            if publish:
                self.temporary.replace(self.path)
        finally:
            self.closed = True
            self.temporary.unlink(missing_ok=True)

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        self.close(publish=kind is None)


def describe(path: Path, primary_key: str | list[str]) -> dict:
    metadata = pq.ParquetFile(path).metadata
    return {"path": path.name, "rows": metadata.num_rows, "bytes": path.stat().st_size,
            "sha256": sha256(path), "primary_key": primary_key,
            "columns": pq.read_schema(path).names}


def seal_stage(directory: Path, stage: str, stage_id: str, parents: dict,
               datasets: dict[str, tuple[Path, str | list[str]]], config: dict) -> dict:
    result = {"version": 1, "status": "COMPLETE", "stage": stage, "stage_id": stage_id,
              "parents": parents, "config_hash": digest(config),
              "datasets": {name: describe(path, primary_key) for name, (path, primary_key) in datasets.items()}}
    write_json(directory / "manifest.json", result)
    return result


def write_features(path: Path, features: list[dict], crs: str, geometry_type: str) -> None:
    """Expose primary/foreign keys as columns and every attribute in properties_json."""
    columns = ["id", "run_id", "refine_id", "detect_id", "clip_stage_id", "fit_id", "network_id",
               "from_intersection_id", "to_intersection_id", "centerline_id"]
    fields = [(name, S) for name in columns] + [("properties_json", S), ("geometry", B)]
    with ParquetSink(path, schema(fields, crs, geometry_type)) as sink:
        for feature in features:
            properties = feature["properties"]
            sink.append({**{name: str(properties[name]) if properties.get(name) is not None else None
                            for name in columns}, "id": str(feature["id"]),
                         "properties_json": json.dumps(properties, ensure_ascii=False, allow_nan=False),
                         "geometry": shape(feature["geometry"]).wkb})


def write_geojson(path: Path, features: list[dict], crs: str) -> None:
    converter = Transformer.from_crs(crs, 4326, always_xy=True).transform
    rows = [{**feature, "geometry": mapping(transform(shape(feature["geometry"]), converter, interleaved=False))}
            for feature in features]
    write_json(path, {"type": "FeatureCollection", "features": rows})
