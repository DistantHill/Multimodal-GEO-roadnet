"""Read one immutable Detect Parquet v1 dataset for Clip."""

from __future__ import annotations

from collections.abc import Iterator
import json
import math
from pathlib import Path
import duckdb

from .artifact_parquet import validate_published
from .db.artifact_registry import ArtifactDescriptor
from .detect_parquet_export import DetectParquetError, _literal, _validate_schema
from .models import ClipIntersection, ClipTrack, ResolvedTrackPoint, RuntimeConfig


class DetectDatasetReader:
    """Own validated, read-only DuckDB views of one Detect v1 publication."""

    def __init__(
        self, publication_root: Path | str, descriptor: ArtifactDescriptor, state: str,
        runtime: RuntimeConfig, *, verify_sha256: bool = False,
    ) -> None:
        validate_published(
            publication_root, descriptor, state, verify_sha256=verify_sha256,
        )
        if descriptor.stage != "detect" or descriptor.detect_id is None:
            raise DetectParquetError("dataset must belong to the detect stage")
        _validate_schema(descriptor)
        self._descriptor = descriptor
        self._runtime = runtime
        self._root = Path(publication_root).resolve(strict=True)
        self._connection: duckdb.DuckDBPyConnection | None = None

    @property
    def descriptor(self) -> ArtifactDescriptor:
        return self._descriptor

    def __enter__(self) -> DetectDatasetReader:
        if self._connection is not None:
            raise DetectParquetError("dataset reader is already open")
        temp = self._root.parent / "duckdb" / str(self._descriptor.detect_id)
        temp.mkdir(parents=True, exist_ok=True)
        self._connection = duckdb.connect(database=":memory:", config={
            "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false",
            "memory_limit": self._runtime.duckdb_memory_limit,
            "threads": str(self._runtime.duckdb_threads),
            "temp_directory": str(temp),
            "max_temp_directory_size": self._runtime.duckdb_max_temp_directory_size,
        })
        try:
            manifest = json.loads(self._descriptor.manifest_json)
            final = self._root / self._descriptor.relative_path
            for table in manifest["tables"]:
                paths = [str(final / item["relative_path"]) for item in table["files"]]
                literals = ", ".join(_literal(path) for path in paths)
                self._connection.execute(
                    f"CREATE TEMP VIEW {table['name']} AS "
                    f"SELECT * FROM read_parquet([{literals}])"
                )
            self._validate_relations()
        except BaseException:
            self._connection.close()
            self._connection = None
            raise
        return self

    def __exit__(self, *_args: object) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def intersections(self) -> tuple[ClipIntersection, ...]:
        """Return the small, stable intersection-circle relation."""
        result = self._require_open().execute(
            "SELECT intersection_id, lon, lat, radius_m "
            "FROM intersections ORDER BY intersection_id"
        )
        items: list[ClipIntersection] = []
        while rows := result.fetchmany(self._runtime.parquet_read_batch_size):
            items.extend(ClipIntersection(int(row[0]), float(row[1]), float(row[2]),
                                          float(row[3])) for row in rows)
        return tuple(items)

    def iter_tracks(self, *, batch_size: int | None = None) -> Iterator[ClipTrack]:
        """Yield one Clip track at a time, retaining at most one subtrack and one batch."""
        batch_size = self._runtime.parquet_read_batch_size if batch_size is None else batch_size
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise DetectParquetError("batch_size is invalid")
        result = self._require_open().execute("""
          SELECT resolved_point_id, subtrack_index, sequence_in_subtrack, vehicle_id,
                 lon, lat, crossing_id, intersection_id, crossing_type
          FROM resolved_points
          ORDER BY subtrack_index, sequence_in_subtrack, resolved_point_id
        """)
        current: int | None = None
        vehicle = ""
        points: list[ResolvedTrackPoint] = []
        while rows := result.fetchmany(batch_size):
            for row in rows:
                subtrack = int(row[1])
                if current is not None and subtrack != current:
                    yield self._track(current, vehicle, points)
                    points, vehicle = [], ""
                current = subtrack
                current_vehicle = str(row[3])
                if vehicle and current_vehicle != vehicle:
                    raise DetectParquetError(f"subtrack {subtrack} contains multiple vehicles")
                vehicle = current_vehicle
                points.append(ResolvedTrackPoint(
                    resolved_point_id=int(row[0]), lon=float(row[4]), lat=float(row[5]),
                    crossing_id=None if row[6] is None else int(row[6]),
                    intersection_id=None if row[7] is None else int(row[7]),
                    crossing_type=None if row[8] is None else int(row[8]),
                ))
        if current is not None:
            yield self._track(current, vehicle, points)

    def _track(self, subtrack: int, vehicle: str,
               points: list[ResolvedTrackPoint]) -> ClipTrack:
        if not vehicle:
            raise DetectParquetError(f"subtrack {subtrack} has no source vehicle")
        return ClipTrack(vehicle, f"{vehicle}:subtrack-{subtrack:04d}", tuple(points), subtrack)

    def _require_open(self) -> duckdb.DuckDBPyConnection:
        if self._connection is None:
            raise DetectParquetError("dataset reader is not open")
        return self._connection

    def _validate_relations(self) -> None:
        connection = self._require_open()
        owner = str(self._descriptor.detect_id)
        invalid = connection.execute("""
          SELECT sum(n) FROM (
            SELECT count(*) n FROM resolved_points WHERE detect_id IS DISTINCT FROM ?::UUID
              OR resolved_point_id IS NULL OR subtrack_index IS NULL
              OR sequence_in_subtrack IS NULL OR vehicle_id IS NULL OR vehicle_id=''
              OR lon IS NULL OR lat IS NULL
              OR NOT isfinite(lon) OR NOT isfinite(lat) OR lon NOT BETWEEN -180 AND 180
              OR lat NOT BETWEEN -90 AND 90 OR point_kind IS NULL
              OR point_kind NOT IN ('kept','injected','crossing')
              OR (point_kind='crossing') != (crossing_id IS NOT NULL
                   AND intersection_id IS NOT NULL AND crossing_type IN (0,1)
                   AND refined_point_id IS NULL AND refined_sequence_in_subtrack IS NULL
                   AND source_row_key IS NULL)
              OR (point_kind IN ('kept','injected')) != (refined_point_id IS NOT NULL
                   AND refined_sequence_in_subtrack IS NOT NULL AND crossing_id IS NULL
                   AND intersection_id IS NULL AND crossing_type IS NULL)
              OR (point_kind='kept') != (source_row_key IS NOT NULL)
            UNION ALL SELECT count(*) FROM intersections
            WHERE detect_id IS DISTINCT FROM ?::UUID OR intersection_id IS NULL
              OR lon IS NULL OR lat IS NULL OR radius_m IS NULL
              OR NOT isfinite(lon) OR NOT isfinite(lat) OR lon NOT BETWEEN -180 AND 180
              OR lat NOT BETWEEN -90 AND 90 OR NOT isfinite(radius_m) OR radius_m<=0
            UNION ALL SELECT count(*) FROM entry_exit_points
            WHERE detect_id IS DISTINCT FROM ?::UUID OR crossing_id IS NULL
              OR intersection_id IS NULL OR resolved_point_id IS NULL
              OR segment_start_resolved_point_id IS NULL OR crossing_type IS NULL
              OR x IS NULL OR y IS NULL OR lon IS NULL OR lat IS NULL
              OR crossing_type NOT IN (0,1) OR NOT isfinite(x) OR NOT isfinite(y)
              OR NOT isfinite(lon) OR NOT isfinite(lat) OR lon NOT BETWEEN -180 AND 180
              OR lat NOT BETWEEN -90 AND 90
          ) checks
        """, [owner, owner, owner]).fetchone()
        sequence_errors = connection.execute("""
          WITH checks AS (
            SELECT subtrack_index, min(sequence_in_subtrack) lo,
                   max(sequence_in_subtrack) hi, count(*) n,
                   count(DISTINCT sequence_in_subtrack) distinct_sequences,
                   count(DISTINCT vehicle_id) vehicles
            FROM resolved_points GROUP BY subtrack_index)
          SELECT count(*) FROM checks
          WHERE lo<>0 OR hi<>n-1 OR distinct_sequences<>n OR vehicles<>1
        """).fetchone()
        relation_errors = connection.execute("""
          WITH rp AS (
            SELECT crossing_id, min(resolved_point_id) resolved_point_id,
                   min(intersection_id) intersection_id, min(crossing_type) crossing_type,
                   min(vehicle_id) vehicle_id, min(subtrack_index) subtrack_index,
                   min(sequence_in_subtrack) sequence_in_subtrack,
                   min(lon) lon, min(lat) lat, count(*) n
            FROM resolved_points WHERE crossing_id IS NOT NULL
            GROUP BY crossing_id), ep AS (
            SELECT crossing_id, min(resolved_point_id) resolved_point_id,
                   min(intersection_id) intersection_id, min(crossing_type) crossing_type,
                   min(segment_start_resolved_point_id) segment_start_resolved_point_id,
                   min(lon) lon, min(lat) lat, count(*) n
            FROM entry_exit_points GROUP BY crossing_id)
          SELECT count(*) FROM rp FULL OUTER JOIN ep USING (crossing_id)
          LEFT JOIN intersections i ON i.intersection_id=coalesce(rp.intersection_id,ep.intersection_id)
          LEFT JOIN resolved_points start ON start.resolved_point_id=ep.segment_start_resolved_point_id
          WHERE rp.n IS DISTINCT FROM 1 OR ep.n IS DISTINCT FROM 1
             OR rp.resolved_point_id IS DISTINCT FROM ep.resolved_point_id
             OR rp.intersection_id IS DISTINCT FROM ep.intersection_id
             OR rp.crossing_type IS DISTINCT FROM ep.crossing_type
             OR rp.lon IS DISTINCT FROM ep.lon OR rp.lat IS DISTINCT FROM ep.lat
             OR i.intersection_id IS NULL
             OR start.resolved_point_id IS NULL OR start.point_kind NOT IN ('kept','injected')
             OR start.vehicle_id IS DISTINCT FROM rp.vehicle_id
             OR start.subtrack_index IS DISTINCT FROM rp.subtrack_index
             OR start.sequence_in_subtrack>=rp.sequence_in_subtrack
        """).fetchone()
        duplicate_errors = connection.execute("""
          SELECT sum(n) FROM (
            SELECT count(*)-count(DISTINCT resolved_point_id) n FROM resolved_points
            UNION ALL SELECT count(*)-count(DISTINCT intersection_id) FROM intersections
            UNION ALL SELECT count(*)-count(DISTINCT crossing_id) FROM entry_exit_points
          ) checks
        """).fetchone()
        counts = (invalid[0], sequence_errors[0], relation_errors[0], duplicate_errors[0])
        if any(value is None or int(value) for value in counts):
            raise DetectParquetError("Detect dataset violates the v1 Clip input semantics")
