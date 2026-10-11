"""Reusable point-exact local Refine fragments without external neighbours."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import logging
from pathlib import Path
import shutil
from uuid import UUID, uuid4

import duckdb
import numpy as np
import polars as pl
from shapely import covers, points
from shapely.geometry import shape

from track.v2.artifacts.parquet_manifest import _parquet_metadata, validate_published
from track.v2.artifacts.publish_dataset import _set_tree_modes
from track.v2.db.artifact_registry import ArtifactDescriptor, canonical_json
from track.v2.time_selection import time_bounds
from track.v2.trajectories.results import RefineParquetError
from track.v2.trajectories.results.parquet_schema import _validate_schema
from track.v2.trajectories.results.read_parquet import RefineDatasetReader

LOGGER = logging.getLogger(__name__)

CACHE_VERSION = "intersection-local-refine-v2"
COMMON_COLUMNS = ("refine_id", "point_id", "vehicle_id", "subtrack_index",
                  "refined_sequence_in_subtrack", "timestamp", "lon", "lat",
                  "x", "y", "heading", "speed")
RAW_SCHEMA = {name: dtype for name, dtype in zip(
    (*COMMON_COLUMNS, "source_row_key", "generated_reason", "after_sequence_in_subtrack", "heading_cal", "point_kind"),
    (pl.String, pl.Int64, pl.String, pl.Int32, pl.Int64, *([pl.Float64] * 7),
     pl.Int64, pl.String, pl.Int32, pl.Float64, pl.String))}

# Parquet has no persisted spatial index. These scalar bounds and half-open
# time predicates run on each manifest-listed source before exact covers and
# before any local ordering/window operation; footer statistics can prune.
SOURCE_SQL = """
SELECT {common}, source_row_key, NULL::VARCHAR AS generated_reason,
       NULL::INTEGER AS after_sequence_in_subtrack, {heading}, 'kept' AS point_kind
FROM read_parquet(?) WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?
  AND (? IS NULL OR timestamp >= ?) AND (? IS NULL OR timestamp < ?)
UNION ALL BY NAME
SELECT {common}, NULL::BIGINT AS source_row_key, generated_reason,
       after_sequence_in_subtrack, {heading}, 'injected' AS point_kind
FROM read_parquet(?) WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?
  AND (? IS NULL OR timestamp >= ?) AND (? IS NULL OR timestamp < ?)
"""

# This relation already contains only exact-ROI points. A missing original
# sequence means an excluded point, so it creates a new local subtrack even
# when the vehicle re-enters the same polygon later.
LOCALIZE_SQL = """
CREATE TEMP TABLE localized AS
WITH previous AS (
  SELECT *, lag(refined_sequence_in_subtrack) OVER parent AS previous_sequence
  FROM read_parquet(?)
  WINDOW parent AS (PARTITION BY vehicle_id, subtrack_index
    ORDER BY refined_sequence_in_subtrack, point_kind, point_id)
), fragmented AS (
  SELECT *, sum(CASE WHEN previous_sequence IS NULL OR
                        refined_sequence_in_subtrack != previous_sequence + 1
                   THEN 1 ELSE 0 END) OVER parent AS fragment
  FROM previous
  WINDOW parent AS (PARTITION BY vehicle_id, subtrack_index
    ORDER BY refined_sequence_in_subtrack, point_kind, point_id)
), numbered AS (
  SELECT *, dense_rank() OVER (ORDER BY vehicle_id, subtrack_index, fragment)-1 AS local_subtrack,
    row_number() OVER (PARTITION BY vehicle_id, subtrack_index, fragment
      ORDER BY refined_sequence_in_subtrack, point_kind, point_id)-1 AS local_sequence
  FROM fragmented
)
SELECT * EXCLUDE (subtrack_index, refined_sequence_in_subtrack, after_sequence_in_subtrack,
                  previous_sequence, fragment, local_subtrack, local_sequence),
       CAST(local_subtrack AS INTEGER) AS subtrack_index,
       local_sequence AS refined_sequence_in_subtrack,
       CASE WHEN point_kind = 'injected' THEN CAST(local_sequence-1 AS INTEGER)
            ELSE NULL END AS after_sequence_in_subtrack,
       subtrack_index AS original_subtrack_index,
       refined_sequence_in_subtrack AS original_refined_sequence_in_subtrack,
       after_sequence_in_subtrack AS original_after_sequence_in_subtrack
FROM numbered
"""
COPY_LOCAL_SQL = """
COPY (SELECT CAST(refine_id AS UUID) AS refine_id, {columns}, {kind_columns}, heading_cal,
      original_subtrack_index, original_refined_sequence_in_subtrack, original_after_sequence_in_subtrack
      FROM localized WHERE point_kind = ?
      ORDER BY vehicle_id, subtrack_index, refined_sequence_in_subtrack)
TO ? (FORMAT PARQUET, COMPRESSION ZSTD)
"""
COUNTS_SQL = "SELECT count(*), count(DISTINCT subtrack_index) FROM localized"
SOURCE_VALIDATION_SQL = """
WITH checked AS (
  SELECT *, count(*) OVER (PARTITION BY vehicle_id, subtrack_index,
                            refined_sequence_in_subtrack) AS occurrences,
            count(DISTINCT vehicle_id) OVER (PARTITION BY subtrack_index) AS vehicles
  FROM read_parquet(?)
)
SELECT count(*) FROM checked
WHERE vehicle_id IS NULL OR vehicle_id = '' OR subtrack_index IS NULL OR subtrack_index < 0
   OR refined_sequence_in_subtrack IS NULL OR refined_sequence_in_subtrack < 0
   OR occurrences != 1 OR vehicles != 1
"""


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _descriptor_record(descriptor):
    return {key: str(value) if isinstance(value, UUID) else value for key, value in asdict(descriptor).items()}


def _descriptor_from_record(record):
    value = dict(record)
    for key in ("id", "refine_id", "detect_id"):
        if value[key] is not None and not isinstance(value[key], UUID):
            value[key] = UUID(value[key])
    return ArtifactDescriptor(**value)


def _load_cache(root: Path, identity: dict) -> dict:
    record = json.loads((root / "cache_manifest.json").read_text())
    if record["identity"] != identity:
        raise RefineParquetError("local Refine cache identity differs from requested inputs")
    for level in record["levels"].values():
        descriptor = _descriptor_from_record(level["descriptor"])
        base = Path(level["publication_root"])
        validate_published(base, descriptor, "present", verify_sha256=True)
        level["descriptor"] = descriptor
    return record


class LocalRefineReader(RefineDatasetReader):
    """Read a validated local cache with the ordinary Detect reader interface."""

    def __init__(self, level_cache: dict, runtime, *, data_root=None):
        descriptor = level_cache["descriptor"]
        if not isinstance(descriptor, ArtifactDescriptor):
            descriptor = _descriptor_from_record(descriptor)
        # Local schemas add original identities to the normal kept/injected
        # layout. They were validated at cache creation, not against the parent
        # schema or by a repeated full-parent row scan for each trial.
        validate_published(level_cache["publication_root"], descriptor, "present")
        self._schema_version = 2
        self.time_start = self.time_end = self.bbox = None
        self._descriptor, self._runtime = descriptor, runtime
        self._root = Path(level_cache["publication_root"]).resolve(strict=True)
        self._data_root = Path(data_root).resolve() if data_root is not None else self._root
        if not self._data_root.is_absolute() or self._data_root == Path(self._data_root.anchor):
            raise RefineParquetError("local reader requires an absolute non-root data directory")
        self._connection = self._temp_directory = None
        self._source_validated = True


def prepare_local_refine(publication_root, descriptor: ArtifactDescriptor, state: str, runtime,
                         inputs_manifest: dict, output_root, *, time_start=None, time_end=None,
                         batch_size: int | None = None) -> dict:
    """Scan each parent point table once and publish reusable level caches.

    Parent ID, manifest hash, fixed inputs hash, time bounds and cache code
    version define the cache identity. Reuse checks local file hashes only.
    """
    start, end = time_bounds(time_start, time_end)
    size = runtime.parquet_read_batch_size if batch_size is None else batch_size
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise ValueError("batch_size must be a positive integer")
    regions = {name: shape(level["computation_region"]) for name, level in inputs_manifest["levels"].items()}
    if not regions or any(g.is_empty or not g.is_valid or g.geom_type not in ("Polygon", "MultiPolygon") for g in regions.values()):
        raise ValueError("local computation regions must be valid nonempty polygons")
    canonical_inputs = {key: value for key, value in inputs_manifest.items() if key != "sha256"}
    inputs_hash = hashlib.sha256(canonical_json(json.loads(json.dumps(canonical_inputs))).encode()).hexdigest()
    if inputs_manifest.get("sha256") != inputs_hash:
        raise ValueError("local inputs manifest hash does not match its contents")
    identity = {"version": CACHE_VERSION, "parent_refine_id": str(descriptor.refine_id),
                "parent_manifest_sha256": descriptor.manifest_sha256, "inputs_sha256": inputs_hash,
                "time_start": start.isoformat() if start else None, "time_end": end.isoformat() if end else None}
    key = hashlib.sha256(canonical_json(identity).encode()).hexdigest()
    target = Path(output_root).resolve() / key
    if (target / "cache_manifest.json").exists():
        LOGGER.info("Reusing verified local Refine cache %s", key)
        return _load_cache(target, identity)
    LOGGER.info("Preparing local Refine cache: parent=%s levels=%s", descriptor.refine_id, sorted(regions))
    validate_published(publication_root, descriptor, state)
    version = _validate_schema(descriptor)
    parent = Path(publication_root) / descriptor.relative_path
    tables = {table["name"]: table for table in json.loads(descriptor.manifest_json)["tables"]}
    paths = {name: [str(parent / f["relative_path"]) for f in tables[name]["files"]]
             for name in ("kept_points", "injected_points")}
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{key}-{uuid4().hex}"
    staging.mkdir()
    raw = {name: [] for name in regions}
    empty = pl.DataFrame(schema=RAW_SCHEMA)
    temp = staging / "duckdb"
    temp.mkdir()
    connection = duckdb.connect(config={"memory_limit": runtime.duckdb_memory_limit,
        "threads": str(runtime.duckdb_threads), "temp_directory": str(temp),
        "max_temp_directory_size": runtime.duckdb_max_temp_directory_size,
        "autoinstall_known_extensions": "false", "autoload_known_extensions": "false"})
    try:
        west = min(g.bounds[0] for g in regions.values())
        south = min(g.bounds[1] for g in regions.values())
        east = max(g.bounds[2] for g in regions.values())
        north = max(g.bounds[3] for g in regions.values())
        bounds_time = [west, east, south, north, start.timestamp() if start else None,
                       start.timestamp() if start else None, end.timestamp() if end else None,
                       end.timestamp() if end else None]
        sql = SOURCE_SQL.format(common=", ".join(COMMON_COLUMNS),
                                heading="heading_cal" if version == 2 else "NULL::DOUBLE AS heading_cal")
        cursor = connection.execute(sql, [paths["kept_points"], *bounds_time, paths["injected_points"], *bounds_time])
        batch_index = 0
        bounded_points = 0
        selected_counts = {name: 0 for name in regions}
        while rows := cursor.fetchmany(size):
            bounded_points += len(rows)
            coordinates = np.asarray([(row[6], row[7]) for row in rows], dtype=float)
            point_geometries = points(coordinates)
            normalized = [(str(row[0]), *row[1:]) for row in rows]
            for name, region in regions.items():
                indices = np.flatnonzero(covers(region, point_geometries))
                if not len(indices):
                    continue
                selected_counts[name] += len(indices)
                selected = [normalized[i] for i in indices]
                if any(row[0] != str(descriptor.refine_id) for row in selected):
                    raise RefineParquetError("selected local point belongs to a different parent Refine")
                directory = staging / name / "raw"
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / f"{batch_index}.parquet"
                pl.DataFrame(selected, schema=RAW_SCHEMA, orient="row").write_parquet(path)
                raw[name].append(str(path))
            batch_index += 1
            if batch_index % 100 == 0:
                LOGGER.info("Local input scan: bbox_time_points=%d selected=%s", bounded_points, selected_counts)
        LOGGER.info("Local input scan completed: bbox_time_points=%d selected=%s", bounded_points, selected_counts)
        levels = {}
        for name in regions:
            if not raw[name]:
                directory = staging / name / "raw"
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / "empty.parquet"
                empty.write_parquet(path)
                raw[name].append(str(path))
            if connection.execute(SOURCE_VALIDATION_SQL, [raw[name]]).fetchone()[0]:
                raise RefineParquetError("local source has corrupt original identity or point sequences")
            connection.execute(LOCALIZE_SQL, [raw[name]])
            point_count, fragment_count = connection.execute(COUNTS_SQL).fetchone()
            LOGGER.info("Local %s fragments prepared: points=%d fragments=%d", name, point_count, fragment_count)
            level_root = staging / name
            final = level_root / descriptor.relative_path
            final.mkdir(parents=True)
            local_tables = []
            for table_name, kind in (("kept_points", "kept"), ("injected_points", "injected")):
                directory = final / table_name
                directory.mkdir()
                path = directory / "points.parquet"
                kind_columns = "source_row_key" if kind == "kept" else "generated_reason, after_sequence_in_subtrack"
                # DuckDB binds COPY's destination parameter before parameters
                # inside its SELECT, despite their textual order.
                connection.execute(COPY_LOCAL_SQL.format(columns=", ".join(COMMON_COLUMNS[1:]),
                                                         kind_columns=kind_columns), [str(path), kind])
                count, schema = _parquet_metadata(connection, path)
                local_tables.append({"name": table_name, "schema": schema,
                    "schema_fingerprint": hashlib.sha256(canonical_json(schema).encode()).hexdigest(),
                    "row_count": count, "files": [{"relative_path": f"{table_name}/points.parquet",
                        "byte_count": path.stat().st_size, "sha256": _digest(path)}]})
            local_descriptor = ArtifactDescriptor.create(id=uuid4(), stage="refine", refine_id=descriptor.refine_id,
                relative_path=descriptor.relative_path, source_fingerprint=identity,
                manifest={"version": 1, "tables": local_tables})
            _set_tree_modes(final, readonly=True)
            level = {"publication_root": str(level_root), "descriptor": local_descriptor,
                     "point_count": point_count, "fragment_count": fragment_count}
            # Validate rows and identities once on the local cache. Trial
            # readers reuse this result, never revalidate all parent points.
            with LocalRefineReader(level, runtime) as reader:
                reader._source_validated = False
                reader._validate_source(reader._connection)
            levels[name] = {**level, "publication_root": str(target / name),
                            "descriptor": _descriptor_record(local_descriptor)}
            connection.execute("DROP TABLE localized")
            shutil.rmtree(level_root / "raw")
        connection.close()
        connection = None
        shutil.rmtree(temp)
        record = {"cache_key": key, "identity": identity, "levels": levels}
        (staging / "cache_manifest.json").write_text(canonical_json(record) + "\n")
        if target.exists():
            raise RefineParquetError("incomplete or concurrent local Refine cache already exists")
        staging.rename(target)
        LOGGER.info("Local Refine cache published: %s", key)
        return _load_cache(target, identity)
    finally:
        if connection is not None:
            connection.close()
        if staging.exists():
            _set_tree_modes(staging, readonly=False)
            shutil.rmtree(staging)
