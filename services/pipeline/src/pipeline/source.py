"""Normalize input once; all subsequent computation reads its Parquet snapshot.

DuckDB provides external sorting and predicate pushdown. Python only converts
records/CRS and attaches immutable provenance; no trajectory algorithms live here.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import fcntl
import glob
import json
import math
import os
from pathlib import Path
import shutil
from typing import Iterator
import uuid
from zoneinfo import ZoneInfo

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from pyproj import CRS, Transformer
import shapely

from .artifacts import SOURCE_FIELDS, ParquetSink, digest, key, schema, sha256, write_json


@dataclass(frozen=True)
class PreparedSource:
    path: Path
    identity: dict
    rows: int
    rejected_rows: int
    crs: str


def connection(config: dict, temporary_root: Path):
    temporary_root.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(config={
        "memory_limit": f"{config['resources']['duckdb_memory_mb']}MB",
        "threads": config["resources"]["threads"],
        "temp_directory": str(temporary_root), "preserve_insertion_order": False,
    })


def _files(config: dict) -> list[Path]:
    value = Path(config["input"]["path"])
    if value.is_dir():
        suffix = "*.parquet" if config["input"]["kind"] == "parquet" else "*.csv"
        files = sorted(value.rglob(suffix))
    else:
        files = [Path(name) for name in sorted(glob.glob(str(value)))]
    if not files or any(not path.is_file() for path in files):
        raise ValueError(f"No {config['input']['kind']} input files match {value}")
    return files


def _scalar(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, bytes):
        return {"encoding": "hex", "data": value.hex()}
    if isinstance(value, (list, tuple)):
        return [_scalar(item) for item in value]
    if isinstance(value, dict):
        return {str(name): _scalar(item) for name, item in value.items()}
    return str(value)


def _timestamp(value, unit: str, timezone: ZoneInfo) -> float:
    if isinstance(value, datetime):
        date = value
    elif unit != "iso":
        return float(value) / {"s": 1, "ms": 1000, "us": 1000000, "ns": 1000000000}[unit]
    else:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone)
    return date.timestamp()


def _number(value) -> float | None:
    if value is None or value == "":
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _raw_batches(config: dict, files: list[Path], directory: Path) -> Iterator[list[dict]]:
    settings, count = config["input"], config["resources"]["batch_rows"]
    if settings["kind"] == "postgres":
        import psycopg
        from psycopg import sql
        dsn = os.environ.get(settings["dsn_env"])
        if not dsn:
            raise ValueError(f"Set the connection secret in {settings['dsn_env']}")
        names = settings["table"].split(".")
        if len(names) not in (1, 2) or any(not name for name in names):
            raise ValueError("input.table must be table or schema.table")
        # Full immutable snapshot export: intentionally no spatial predicate.
        # The source connection never creates or modifies any source table.
        with psycopg.connect(dsn) as database:
            database.read_only = True
            database.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            with database.cursor(name="workbench_source_snapshot") as cursor:
                cursor.itersize = count
                cursor.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(*names)))
                columns = [item.name for item in cursor.description]
                while rows := cursor.fetchmany(count):
                    yield [dict(zip(columns, row, strict=True)) for row in rows]
        return
    with connection(config, directory / "raw-spill") as database:
        if settings["kind"] == "csv":
            relation = database.read_csv([str(path) for path in files], all_varchar=True, union_by_name=True)
        else:
            relation = database.read_parquet([str(path) for path in files], union_by_name=True)
        for batch in relation.to_arrow_reader(count):
            yield batch.to_pylist()


@contextmanager
def _snapshot_lock(root: Path) -> Iterator[None]:
    """Serialize cache publication and make waiting processes recheck the manifest.

    Keep the lock file: unlinking it permits concurrent locks on different inodes.
    The OS releases the lock if a writer fails or its process exits.
    """
    root.mkdir(parents=True, exist_ok=True)
    with (root / "prepare.lock").open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def prepare_source(config: dict, output_root: Path, monitor=None) -> PreparedSource:
    """Hash source bytes, import all attributes, reject invalid coordinates, sort.

    PostgreSQL is exported on every invocation because metadata cannot prove an
    unchanged live table. A selected snapshot can subsequently be used as a file.
    """
    output_root = Path(output_root)
    settings = config["input"]
    files = [] if settings["kind"] == "postgres" else _files(config)
    input_files = [{"path": str(path.resolve()), "bytes": path.stat().st_size, "sha256": sha256(path)} for path in files]
    identity = {"input": settings, "files": input_files, "schema_version": 1}
    if not files:
        identity["snapshot_nonce"] = uuid.uuid4().hex
    root = output_root / "inputs" / digest(identity)
    with _snapshot_lock(root):
        marker = root / "manifest.json"
        if marker.is_file():
            prior = json.loads(marker.read_text())
            path = root / "points.parquet"
            if prior["status"] != "COMPLETE" or sha256(path) != prior["sha256"]:
                raise ValueError("Input snapshot checksum failed; remove damaged snapshot and re-import")
            return PreparedSource(path, prior, prior["rows"], prior["rejected_rows"], prior["crs"])
        raw = root / f"normalizing-{uuid.uuid4().hex}.parquet"
        fields = settings["columns"]
        source_crs = CRS(settings["source_crs"])
        crs = settings["processing_crs"]
        projector = Transformer.from_crs(source_crs, crs, always_xy=True)
        geographic = Transformer.from_crs(crs, 4326, always_xy=True)
        timezone = ZoneInfo(settings["timezone"])
        point_schema = schema(SOURCE_FIELDS, crs, "Point")
        rejects_schema = pa.schema([("source_id", pa.string()), ("reason", pa.string()), ("attributes_json", pa.string())])
        rejected = 0
        raw_count = 0
        try:
            with ParquetSink(raw, point_schema, config["resources"]["batch_rows"]) as sink, ParquetSink(root / "rejected.parquet", rejects_schema) as bad:
                for rows in _raw_batches(config, files, root):
                    if monitor:
                        monitor.check()
                    normalized = []
                    for row in rows:
                        raw_count += 1
                        attributes = json.dumps(_scalar(row), ensure_ascii=False, allow_nan=False)
                        required = ("id", "vehicle_id", "timestamp", "x", "y")
                        missing = [fields[name] for name in required if fields[name] not in row]
                        if missing:
                            raise ValueError(f"Missing input columns: {missing}; fix [input.columns]")
                        source_id = str(row[fields["id"]]) if row[fields["id"]] is not None else ""
                        vehicle = str(row[fields["vehicle_id"]]) if row[fields["vehicle_id"]] is not None else ""
                        try:
                            if not source_id or not vehicle:
                                raise ValueError("missing_primary_key_or_vehicle")
                            x, y = float(row[fields["x"]]), float(row[fields["y"]])
                            if not math.isfinite(x) or not math.isfinite(y):
                                raise ValueError("nonfinite_coordinate")
                            if source_crs.is_geographic and not (-180 <= x <= 180 and -90 <= y <= 90):
                                raise ValueError("coordinate_out_of_range")
                            timestamp = _timestamp(row[fields["timestamp"]], settings["timestamp_unit"], timezone)
                            if not math.isfinite(timestamp):
                                raise ValueError("nonfinite_timestamp")
                            heading = _number(row.get(fields["heading"])) if fields["heading"] else None
                            if heading is not None:
                                heading %= 360
                            speed = _number(row.get(fields["speed"])) if fields["speed"] else None
                            if speed is not None:
                                if speed < 0:
                                    raise ValueError("negative_speed")
                                speed /= 3.6 if settings["speed_unit"] == "km/h" else 1.0
                            normalized.append({"source_point_id": key(settings["dataset_id"], source_id),
                                               "source_id": source_id, "vehicle_id": vehicle,
                                               "vehicle_code": int(key(vehicle)[:15], 16), "source_seq": 0,
                                               "timestamp": timestamp, "x": x, "y": y,
                                               "heading": heading, "speed": speed, "attributes_json": attributes})
                        except (ValueError, TypeError, OverflowError) as error:
                            bad.append({"source_id": source_id, "reason": str(error), "attributes_json": attributes})
                            rejected += 1
                    if normalized:
                        x, y = projector.transform([row["x"] for row in normalized], [row["y"] for row in normalized])
                        lon, lat = geographic.transform(x, y)
                        valid = np.isfinite(x) & np.isfinite(y) & np.isfinite(lon) & np.isfinite(lat)
                        geometries = shapely.to_wkb(shapely.points(np.column_stack((x, y))))
                        for index, row in enumerate(normalized):
                            if not valid[index]:
                                bad.append({"source_id": row["source_id"], "reason": "projection_failed", "attributes_json": row["attributes_json"]})
                                rejected += 1
                            else:
                                sink.append({**row, "x": float(x[index]), "y": float(y[index]),
                                             "lon": float(lon[index]), "lat": float(lat[index]), "geometry": geometries[index]})
            with connection(config, root / "sort-spill") as database:
                duplicate = database.execute("SELECT source_id FROM read_parquet(?) GROUP BY source_id HAVING count(*) > 1 LIMIT 1", [str(raw)]).fetchall()
                if duplicate:
                    raise ValueError(f"Duplicate source primary key {duplicate[0][0]!r}; input keys must be globally unique")
                collision = database.execute("SELECT vehicle_code FROM read_parquet(?) GROUP BY vehicle_code HAVING count(DISTINCT vehicle_id)>1 LIMIT 1", [str(raw)]).fetchall()
                if collision:
                    raise ValueError("Vehicle identity hash collision; change dataset identifiers")
                reader = database.sql("SELECT * EXCLUDE(source_seq), row_number() OVER (PARTITION BY vehicle_id ORDER BY timestamp, source_id)-1 AS source_seq FROM read_parquet(?) ORDER BY vehicle_id, timestamp, source_id", params=[str(raw)]).to_arrow_reader(config["resources"]["batch_rows"])
                with ParquetSink(root / "points.parquet", point_schema, config["resources"]["batch_rows"]) as sorted_sink:
                    for batch in reader:
                        if monitor:
                            monitor.check()
                        sorted_sink.extend(batch.to_pylist())
                count = sorted_sink.count
            if not count:
                raise ValueError("No valid input points remain; inspect rejected.parquet and column/CRS settings")
            # Detect edits made while the snapshot was being read, not just at startup.
            if files and any(sha256(path) != entry["sha256"] for path, entry in zip(files, input_files, strict=True)):
                raise ValueError("Input changed during normalization; retry with an immutable source")
            identity.update(status="COMPLETE", rows=count, source_rows=raw_count, rejected_rows=rejected,
                            crs=crs, sha256=sha256(root / "points.parquet"))
            write_json(marker, identity)
            return PreparedSource(root / "points.parquet", identity, count, rejected, crs)
        finally:
            raw.unlink(missing_ok=True)
            for name in ("raw-spill", "sort-spill"):
                shutil.rmtree(root / name, ignore_errors=True)


def iter_groups(database, query: str, parameters: list, group_fields: tuple[str, ...],
                batch_rows: int, limit: int, weight_field: str | None = None) -> Iterator[list[dict]]:
    """Never split a vehicle/pair at an Arrow batch boundary; bounded carry-over."""
    current = None
    group: list[dict] = []
    size = 0
    for batch in database.sql(query, params=parameters).to_arrow_reader(batch_rows):
        for row in batch.to_pylist():
            identity = tuple(row[name] for name in group_fields)
            if current is not None and identity != current:
                yield group
                group = []
                size = 0
            current = identity
            group.append(row)
            size += len(row[weight_field]) if weight_field else 1
            if size > limit:
                from .resources import ResourceLimitExceeded
                raise ResourceLimitExceeded(f"group {identity!r} exceeds configured maximum of {limit} {'points' if weight_field else 'rows'}")
    if group:
        yield group
