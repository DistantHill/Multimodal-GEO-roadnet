"""Read and validate persisted Refine trajectory results from Parquet v1."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import shutil
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import duckdb
import numpy as np
from ..subtrack_columns import refinement_subtrack_from_columns

from track.v2.artifacts.parquet_manifest import validate_published
from track.v2.artifacts.parquet_sql import quote_sql_literal
from track.v2.db.artifact_registry import ArtifactDescriptor
from track.v2.projection import SpatialWindow
from track.v2.models import RuntimeConfig, RefinementSubtrack, TurnBatch
from track.v2._geometry import _angle_delta_deg, _circular_mean_deg

from . import RefineParquetError
from .parquet_schema import _validate_schema

@dataclass(frozen=True, slots=True)
class TopologyTrackSelection:
    window_indices: tuple[int, ...]
    track_key: str
    points: RefinementSubtrack


class RefineDatasetReader:
    """Own a validated, read-only DuckDB view of one Refine v1 dataset."""

    def __init__(
        self, publication_root: Path | str, descriptor: ArtifactDescriptor, state: str,
        runtime: RuntimeConfig, *, verify_sha256: bool = False,
        data_root: Path | str | None = None,
    ) -> None:
        validate_published(
            publication_root, descriptor, state, verify_sha256=verify_sha256,
        )
        if descriptor.stage != "refine" or descriptor.refine_id is None:
            raise RefineParquetError("dataset must belong to the refine stage")
        _validate_schema(descriptor)
        self._descriptor = descriptor
        self._runtime = runtime
        self._root = Path(publication_root).resolve(strict=True)
        configured_root = Path(data_root) if data_root is not None else self._root.parent
        if not configured_root.is_absolute():
            raise RefineParquetError("data_root must be absolute")
        self._data_root = configured_root.resolve()
        if self._data_root == Path(self._data_root.anchor):
            raise RefineParquetError("data_root must not be a filesystem root")
        self._temp_directory: Path | None = None
        self._connection: duckdb.DuckDBPyConnection | None = None
        self._source_validated = False

    @property
    def descriptor(self) -> ArtifactDescriptor:
        """Return the validated immutable descriptor for provenance checks."""
        return self._descriptor

    def __enter__(self) -> RefineDatasetReader:
        if self._connection is not None:
            raise RefineParquetError("dataset reader is already open")
        run_id = self._descriptor.refine_id
        temp = (self._data_root / "duckdb" / "refine-readers" /
                f"{run_id}-{uuid4().hex}")
        self._temp_directory = temp
        try:
            temp.mkdir(parents=True, exist_ok=False)
            self._connection = duckdb.connect(database=":memory:", config={
                "autoinstall_known_extensions": "false",
                "autoload_known_extensions": "false",
                "memory_limit": self._runtime.duckdb_memory_limit,
                "threads": str(self._runtime.duckdb_threads),
                "temp_directory": str(temp),
                "max_temp_directory_size": self._runtime.duckdb_max_temp_directory_size,
            })
            manifest = json.loads(self._descriptor.manifest_json)
            final = self._root / self._descriptor.relative_path
            for table in manifest["tables"]:
                paths = [str(final / item["relative_path"]) for item in table["files"]]
                literals = ", ".join(quote_sql_literal(path) for path in paths)
                self._connection.execute(
                    f"CREATE TEMP VIEW {table['name']} AS "
                    f"SELECT * FROM read_parquet([{literals}])"
                )
            self._connection.execute("""
                CREATE TEMP VIEW refine_points AS
                SELECT *, 'kept'::VARCHAR AS point_kind,
                       NULL::VARCHAR AS generated_reason,
                       NULL::INTEGER AS after_sequence_in_subtrack
                FROM kept_points
                UNION ALL BY NAME
                SELECT *, 'injected'::VARCHAR AS point_kind,
                       NULL::VARCHAR AS source_row_key
                FROM injected_points
            """)
        except BaseException:
            if self._connection is not None:
                self._connection.close()
            self._connection = None
            shutil.rmtree(temp, ignore_errors=True)
            self._temp_directory = None
            raise
        return self

    def __exit__(self, *_args: object) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._temp_directory is not None:
            shutil.rmtree(self._temp_directory, ignore_errors=True)
            self._temp_directory = None

    def iter_subtrack_batches(
        self, vehicle_id: str, subtrack_index: int, *, batch_size: int | None = None,
    ) -> Iterator[tuple[tuple[Any, ...], ...]]:
        """Yield bounded row batches ordered across kept and injected points."""
        if self._connection is None:
            raise RefineParquetError("dataset reader is not open")
        if not isinstance(vehicle_id, str) or not vehicle_id or isinstance(subtrack_index, bool):
            raise RefineParquetError("vehicle_id and subtrack_index are required")
        batch_size = self._runtime.parquet_read_batch_size if batch_size is None else batch_size
        if (not isinstance(subtrack_index, int) or isinstance(batch_size, bool)
                or not isinstance(batch_size, int) or batch_size < 1):
            raise RefineParquetError("subtrack_index and batch_size are invalid")
        query = """
          SELECT refine_id, point_id, vehicle_id, subtrack_index,
                 refined_sequence_in_subtrack, timestamp, lon, lat, x, y,
                 heading, speed, point_kind, source_row_key, generated_reason,
                 after_sequence_in_subtrack
          FROM refine_points points
          WHERE vehicle_id = ? AND subtrack_index = ?
          ORDER BY refined_sequence_in_subtrack, point_kind, point_id
        """
        result = self._connection.execute(query, [vehicle_id, subtrack_index])
        while rows := result.fetchmany(batch_size):
            yield tuple(rows)

    def extent(self) -> tuple[float, float, float, float] | None:
        """Return processing-CRS bounds without materializing dataset rows."""
        connection = self._require_open()
        row = connection.execute("""
          SELECT min(x), min(y), max(x), max(y) FROM refine_points
        """).fetchone()
        if row is None or row[0] is None:
            return None
        extent = tuple(float(value) for value in row)
        if not all(math.isfinite(value) for value in extent):
            raise RefineParquetError("dataset extent contains non-finite coordinates")
        return extent  # type: ignore[return-value]

    def track_count(self) -> int:
        """Return the exact number of validated vehicle/subtrack identities."""
        connection = self._require_open()
        self._validate_source(connection)
        row = connection.execute("""
          SELECT count(*) FROM (
            SELECT DISTINCT vehicle_id, subtrack_index FROM refine_points
          ) tracks
        """).fetchone()
        if row is None:
            raise RefineParquetError("could not count source tracks")
        return int(row[0])

    def iter_tracks(
        self, source_table: str, *, batch_size: int | None = None,
    ) -> Iterator[RefinementSubtrack]:
        """Yield validated Detect input, retaining at most one track and one batch."""
        connection = self._require_open()
        if not isinstance(source_table, str) or not source_table.strip():
            raise RefineParquetError("source_table is required")
        batch_size = self._runtime.parquet_read_batch_size if batch_size is None else batch_size
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise RefineParquetError("batch_size is invalid")
        self._validate_source(connection)
        result = connection.execute("""
          SELECT refine_id, point_id, vehicle_id, subtrack_index,
                 refined_sequence_in_subtrack, timestamp, lon, lat, x, y,
                 heading, speed, point_kind, source_row_key, generated_reason,
                 after_sequence_in_subtrack,
                 count(DISTINCT vehicle_id) OVER (PARTITION BY subtrack_index)
          FROM refine_points points
          ORDER BY vehicle_id, subtrack_index, refined_sequence_in_subtrack,
                   point_kind, point_id
        """)
        current_key: int | None = None
        track_rows: list[tuple[Any, ...]] = []
        while rows := result.fetchmany(batch_size):
            for row in rows:
                key = int(row[3])
                if current_key is not None and key != current_key:
                    yield self._decode_track(track_rows, source_table)
                    track_rows = []
                current_key = key
                track_rows.append(row)
        if track_rows:
            yield self._decode_track(track_rows, source_table)

    def iter_topology_tracks(
        self, source_table: str, windows: Iterable[SpatialWindow], *,
        batch_size: int | None = None,
    ) -> Iterator[TopologyTrackSelection]:
        """Yield complete subtracks whose adjacent segments intersect each bbox."""
        connection = self._require_open()
        if not isinstance(source_table, str) or not source_table.strip():
            raise RefineParquetError("source_table is required")
        batch_size = self._runtime.parquet_read_batch_size if batch_size is None else batch_size
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise RefineParquetError("batch_size is invalid")
        canonical: list[tuple[int, tuple[int, ...], float, float, float, float]] = []
        positions: dict[tuple[float, float, float, float], int] = {}
        for input_index, window in enumerate(windows):
            try:
                bounds = tuple(float(value) for value in
                               (window.x_min, window.y_min, window.x_max, window.y_max))
            except (AttributeError, TypeError, ValueError) as exc:
                raise RefineParquetError("topology window is invalid") from exc
            if (not all(math.isfinite(value) for value in bounds)
                    or any(isinstance(value, bool) for value in
                           (window.x_min, window.y_min, window.x_max, window.y_max))
                    or bounds[0] > bounds[2] or bounds[1] > bounds[3]):
                raise RefineParquetError("topology window is invalid")
            position = positions.get(bounds)
            if position is None:
                positions[bounds] = len(canonical)
                canonical.append((len(canonical), (input_index,), *bounds))
            else:
                item = canonical[position]
                canonical[position] = (item[0], item[1] + (input_index,), *item[2:])
        if not canonical:
            return
        self._validate_source(connection)
        table = f"topology_windows_{uuid4().hex}"
        try:
            connection.execute(f"""
              CREATE TEMP TABLE {table}(
                window_order INTEGER, x_min DOUBLE, y_min DOUBLE,
                x_max DOUBLE, y_max DOUBLE
              )
            """)
            connection.executemany(
                f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?)",
                [(item[0], *item[2:]) for item in canonical],
            )
            result = connection.execute(f"""
              WITH adjacent AS (
                SELECT vehicle_id, subtrack_index, x, y,
                       lead(x) OVER track AS end_x, lead(y) OVER track AS end_y
                FROM refine_points
                WINDOW track AS (PARTITION BY vehicle_id, subtrack_index ORDER BY
                  refined_sequence_in_subtrack, point_kind, point_id)
              ), selected AS (
                SELECT DISTINCT w.window_order, a.vehicle_id, a.subtrack_index
                FROM {table} w JOIN adjacent a
                  ON a.end_x IS NOT NULL
                 AND least(a.x, a.end_x) <= w.x_max
                 AND greatest(a.x, a.end_x) >= w.x_min
                 AND least(a.y, a.end_y) <= w.y_max
                 AND greatest(a.y, a.end_y) >= w.y_min
              )
              SELECT p.refine_id, p.point_id, p.vehicle_id, p.subtrack_index,
                     p.refined_sequence_in_subtrack, p.timestamp, p.lon, p.lat,
                     p.x, p.y, p.heading, p.speed, p.point_kind, p.source_row_key,
                     p.generated_reason, p.after_sequence_in_subtrack, 1 AS vehicles,
                     s.window_order
              FROM selected s JOIN refine_points p USING (vehicle_id, subtrack_index)
              ORDER BY s.window_order, p.vehicle_id,
                       CAST(p.subtrack_index AS VARCHAR),
                       p.refined_sequence_in_subtrack, p.point_kind, p.point_id
            """)
            current: tuple[int, str, int] | None = None
            points: list[tuple[Any, ...]] = []
            for rows in iter(lambda: result.fetchmany(batch_size), []):
                for row in rows:
                    key = (int(row[17]), str(row[2]), int(row[3]))
                    if current is not None and key != current:
                        yield TopologyTrackSelection(
                            canonical[current[0]][1], str(current[2]), self._decode_track(points, source_table),
                        )
                        points = []
                    current = key
                    points.append(row[:17])
            if current is not None:
                yield TopologyTrackSelection(
                    canonical[current[0]][1], str(current[2]), self._decode_track(points, source_table),
                )
        finally:
            if self._connection is not None:
                connection.execute(f"DROP TABLE IF EXISTS {table}")

    def iter_turn_candidates(self, source_table: str, cfg: Any, *, batch_size: int | None = None):
        """Yield turn business batches from each complete ordered subtrack."""
        from ...candidates.detect_turn_events import turns_from_subtrack
        if isinstance(cfg.turn_window, bool) or cfg.turn_window < 1:
            raise RefineParquetError("turn_window must be a positive integer")
        thresholds=(cfg.min_turn_deg,cfg.max_turn_deg,cfg.min_leg_m,cfg.min_movement_speed_mps)
        if not all(math.isfinite(value) and value>=0 for value in thresholds) or cfg.min_turn_deg>cfg.max_turn_deg:
            raise RefineParquetError("turn thresholds are invalid")
        offset=0
        tracks=self.iter_tracks(source_table,batch_size=batch_size)
        try:
            for track in tracks:
                batch=turns_from_subtrack(track,cfg,first_index=offset)
                offset+=len(batch)
                if len(batch): yield batch
        finally:
            tracks.close()

    def iter_cross_track_segments(
        self, source_table: str, cfg: Any, *, batch_size: int | None = None,
    ) -> Iterator[dict[str, np.ndarray]]:
        """Yield straight adjacent segments prepared by spillable DuckDB SQL."""
        batch_size = self._runtime.parquet_read_batch_size if batch_size is None else batch_size
        connection, threshold = self._validate_cross_track_request(
            source_table, cfg, batch_size,
        )
        self._validate_source(connection)
        query = self._cross_track_segment_relation("""
          SELECT segment_id, refine_id, point_id, vehicle_id, subtrack_index,
                 refined_sequence_in_subtrack, timestamp, lon, lat, x, y,
                 heading, speed, point_kind, source_row_key, generated_reason,
                 after_sequence_in_subtrack, 1 AS vehicles,
                 vehicle_rank, track_rank, segment_index, end_x, end_y
          FROM straight
          ORDER BY track_rank, segment_index
        """)
        result = connection.execute(query, [threshold])
        while rows := result.fetchmany(batch_size):
            yield self._decode_segment_columns(rows, source_table)

    def iter_cross_track_pair_batches(
        self, source_table: str, cfg: Any, *, batch_size: int | None = None,
    ) -> Iterator[tuple[dict[str, np.ndarray], np.ndarray, np.ndarray]]:
        """Yield stable conservative intersection pairs for the exact NumPy kernel."""
        batch_size = self._runtime.parquet_read_batch_size if batch_size is None else batch_size
        connection, threshold = self._validate_cross_track_request(
            source_table, cfg, batch_size,
        )
        cell_m = cfg.cross_track_grid_cell_m
        max_cells = cfg.cross_track_max_grid_cells_per_segment
        if (isinstance(cell_m, bool) or not isinstance(cell_m, (int, float))
                or not math.isfinite(cell_m) or cell_m <= 0):
            raise RefineParquetError("cross-track grid cell size is invalid")
        if (isinstance(max_cells, bool) or not isinstance(max_cells, int)
                or max_cells < 1):
            raise RefineParquetError("cross-track maximum grid cells is invalid")
        self._validate_source(connection)
        # These legacy knobs remain validated for configuration compatibility,
        # but AABB overlap is independent of grid size and overflow thresholds.
        min_angle = float(cfg.cross_track_min_angle_deg)
        max_angle = float(cfg.cross_track_max_angle_deg)
        if (not math.isfinite(min_angle) or not math.isfinite(max_angle)
                or not 0 <= min_angle <= max_angle <= 180):
            raise RefineParquetError("cross-track angle range is invalid")
        table = f"cross_track_straight_{uuid4().hex}"
        materialize = self._cross_track_segment_relation("""
          SELECT * FROM straight
        """)
        query = f"""
        WITH paired AS (
          SELECT a.segment_id AS a_id, b.segment_id AS b_id,
                 a.heading AS a_heading, b.heading AS b_heading,
                 a.end_x-a.x AS adx, a.end_y-a.y AS ady,
                 b.end_x-b.x AS bdx, b.end_y-b.y AS bdy,
                 b.x-a.x AS rx, b.y-a.y AS ry,
                 abs((((b.heading-a.heading+180.0) % 360.0 + 360.0)
                      % 360.0)-180.0) AS angle,
                 1.4210854715202004e-14
                   * greatest(1.0, abs(a.heading)+abs(b.heading)+540.0) AS angle_error
          FROM {table} a JOIN {table} b
            ON a.min_x <= b.max_x AND b.min_x <= a.max_x
           AND a.min_y <= b.max_y AND b.min_y <= a.max_y
           AND a.segment_id < b.segment_id
           AND a.subtrack_index != b.subtrack_index
        ), numeric_pairs AS (
          SELECT *,
                 adx*(-bdy)-(-bdx)*ady AS det,
                 rx*(-bdy)-(-bdx)*ry AS ta_num,
                 adx*ry-rx*ady AS tb_num,
                 1.4210854715202004e-14 * greatest(
                   1.0, abs(adx*bdy)+abs(bdx*ady),
                   abs(rx*bdy)+abs(bdx*ry),
                   abs(adx*ry)+abs(rx*ady)
                 ) AS arithmetic_error
          FROM paired
          WHERE abs(a_heading) > 1000000.0 OR abs(b_heading) > 1000000.0
             OR angle BETWEEN ?-angle_error AND ?+angle_error
        ), conservative_pairs AS (
          SELECT * FROM numeric_pairs
          WHERE NOT isfinite(det) OR NOT isfinite(ta_num) OR NOT isfinite(tb_num)
             OR (ta_num BETWEEN least(0.0, det)-arithmetic_error
                                AND greatest(0.0, det)+arithmetic_error
                 AND tb_num BETWEEN least(0.0, det)-arithmetic_error
                                AND greatest(0.0, det)+arithmetic_error)
        )
          SELECT a.* EXCLUDE (min_x, max_x, min_y, max_y),
                 b.* EXCLUDE (min_x, max_x, min_y, max_y)
          FROM conservative_pairs p
          JOIN {table} a ON a.segment_id=p.a_id
          JOIN {table} b ON b.segment_id=p.b_id
          ORDER BY a.vehicle_rank, b.vehicle_rank,
                   a.segment_index, b.segment_index, a.track_rank, b.track_rank
        """
        try:
            connection.execute(f"CREATE TEMP TABLE {table} AS {materialize}", [threshold])
            connection.execute(f"ANALYZE {table}")
            result = connection.execute(query, [min_angle, max_angle])
            while rows := result.fetchmany(batch_size):
                columns=self._decode_segment_columns([segment for row in rows for segment in (row[:23],row[23:])],source_table)
                ids_a=np.arange(0,len(rows)*2,2,dtype=np.int64)
                ids_b = ids_a + 1
                swap = columns["subtrack_index"][ids_a] > columns["subtrack_index"][ids_b]
                yield columns,np.where(swap,ids_b,ids_a),np.where(swap,ids_a,ids_b)
        finally:
            if self._connection is connection:
                connection.execute(f"DROP TABLE IF EXISTS {table}")

    def _validate_cross_track_request(
        self, source_table: str, cfg: Any, batch_size: int,
    ) -> tuple[duckdb.DuckDBPyConnection, float]:
        connection = self._require_open()
        if not isinstance(source_table, str) or not source_table.strip():
            raise RefineParquetError("source_table is required")
        threshold = cfg.cross_track_straightness_deg
        if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                or not math.isfinite(threshold) or not 0 <= threshold <= 180):
            raise RefineParquetError("cross-track straightness threshold is invalid")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise RefineParquetError("batch_size is invalid")
        return connection, float(threshold)

    def _validate_source(self, connection: duckdb.DuckDBPyConnection) -> None:
        if getattr(self, "_source_validated", False):
            return
        invalid = connection.execute("""
          WITH checked AS (
            SELECT *,
              row_number() OVER track - 1 AS expected_sequence,
              count(DISTINCT vehicle_id) OVER (PARTITION BY subtrack_index) AS vehicles
            FROM refine_points
            WINDOW track AS (PARTITION BY vehicle_id, subtrack_index
                             ORDER BY refined_sequence_in_subtrack, point_kind, point_id)
          )
          SELECT count(*) FROM checked
          WHERE refine_id IS DISTINCT FROM ? OR point_id IS NULL
             OR vehicle_id IS NULL OR vehicle_id = ''
             OR subtrack_index IS NULL OR subtrack_index < 0
             OR refined_sequence_in_subtrack IS NULL
             OR refined_sequence_in_subtrack != expected_sequence OR vehicles != 1
             OR NOT coalesce(isfinite(timestamp), false)
             OR NOT coalesce(isfinite(lon), false) OR NOT coalesce(isfinite(lat), false)
             OR NOT coalesce(isfinite(x), false) OR NOT coalesce(isfinite(y), false)
             OR NOT coalesce(isfinite(heading), false)
             OR NOT coalesce(isfinite(speed), false) OR lon NOT BETWEEN -180 AND 180
             OR lat NOT BETWEEN -90 AND 90 OR point_kind IS NULL
             OR point_kind NOT IN ('kept', 'injected')
             OR (point_kind = 'kept' AND
                 (source_row_key IS NULL OR source_row_key = '' OR generated_reason IS NOT NULL
                  OR after_sequence_in_subtrack IS NOT NULL))
             OR (point_kind = 'injected' AND
                 (source_row_key IS NOT NULL OR generated_reason IS NULL
                  OR generated_reason = '' OR after_sequence_in_subtrack IS NULL))
        """, [self._descriptor.refine_id]).fetchone()
        if invalid is None or invalid[0]:
            raise RefineParquetError("source contains corrupt identity or sequence data")
        self._source_validated = True

    @staticmethod
    def _cross_track_segment_relation(select_sql: str) -> str:
        return """
          WITH adjacent AS (
            SELECT *,
              dense_rank() OVER (ORDER BY vehicle_id) - 1 AS vehicle_rank,
              dense_rank() OVER
                (ORDER BY vehicle_id || chr(0) || CAST(subtrack_index AS VARCHAR)) - 1
                AS track_rank,
              row_number() OVER track - 1 AS segment_index,
              lead(x) OVER track AS end_x, lead(y) OVER track AS end_y,
              lead(heading) OVER track AS end_heading
            FROM refine_points
            WINDOW track AS (PARTITION BY vehicle_id, subtrack_index
                             ORDER BY refined_sequence_in_subtrack, point_kind, point_id)
          ), deltas AS (
            SELECT *, (((end_heading-heading+180.0) % 360.0 + 360.0)
                       % 360.0)-180.0 AS heading_delta
            FROM adjacent WHERE end_x IS NOT NULL
          ), straight AS (
            SELECT row_number() OVER (ORDER BY track_rank, segment_index) - 1 AS segment_id,
                   refine_id, point_id, vehicle_id, subtrack_index,
                   refined_sequence_in_subtrack, timestamp, lon, lat, x, y,
                   heading, speed, point_kind, source_row_key, generated_reason,
                   after_sequence_in_subtrack, 1 AS vehicles,
                   vehicle_rank, track_rank, segment_index, end_x, end_y,
                   least(x, end_x) AS min_x, greatest(x, end_x) AS max_x,
                   least(y, end_y) AS min_y, greatest(y, end_y) AS max_y
            FROM deltas WHERE abs(heading_delta) <= ?
          )
        """ + select_sql

    def _decode_segment_columns(self, rows, source_table):
        size=len(rows)
        def column(position,dtype):
            return np.asarray([row[position] for row in rows],dtype=dtype)
        if any(row[1]!=self._descriptor.refine_id for row in rows):
            raise RefineParquetError("segment owner differs from descriptor owner")
        kept=column(13,object)=="kept"
        return {
            "starts":np.column_stack((column(9,np.float64),column(10,np.float64))),
            "ends":np.column_stack((column(21,np.float64),column(22,np.float64))),
            "vehicle_id":column(3,object),"subtrack_index":column(4,np.int64),
            "point_index":column(5,np.int64),"point_kind":np.where(kept,"p","i"),
            "refine_point_id":column(2,np.int64),"refine_point_id_valid":np.ones(size,dtype=np.bool_),
            "source_id":np.full(size,source_table,dtype=object),
            "source_row_key":np.asarray([0 if row[14] is None else int(row[14]) for row in rows],dtype=np.int64),
            "source_row_key_valid":kept,
            "generated_reason":np.asarray([row[15] or "" for row in rows],dtype=object),
            "timestamp":column(6,np.float64),"heading":column(11,np.float64),"speed":column(12,np.float64),
        }

    def _require_open(self) -> duckdb.DuckDBPyConnection:
        if self._connection is None:
            raise RefineParquetError("dataset reader is not open")
        return self._connection

    def _decode_track(self, rows: list[tuple[Any, ...]], source_table: str) -> RefinementSubtrack:
        """Materialize one validated subtrack as arrays, never Point wrappers."""
        size = len(rows)
        def floats(position: int) -> np.ndarray:
            return np.fromiter((float(row[position]) for row in rows), dtype=np.float64, count=size)
        def integers(position: int) -> np.ndarray:
            return np.fromiter((int(row[position]) for row in rows), dtype=np.int64, count=size)
        if not size:
            raise RefineParquetError("cannot decode an empty subtrack")
        if any(row[0] != self._descriptor.refine_id for row in rows):
            raise RefineParquetError("row owner differs from descriptor owner")
        if rows[0][4] != 0:
            raise RefineParquetError("subtrack sequence does not start at zero")
        if not np.array_equal(integers(4), np.arange(size, dtype=np.int64)):
            raise RefineParquetError("subtrack sequence is not contiguous from zero")
        if len({(row[2], row[3]) for row in rows}) != 1:
            raise RefineParquetError("subtrack contains conflicting identity")
        if any(len(row) != 17 or row[12] not in {"kept", "injected"} or not isinstance(row[2], str) or not row[2] for row in rows):
            raise RefineParquetError("row identity is invalid")
        if any(row[16] != 1 for row in rows):
            raise RefineParquetError("subtrack contains multiple vehicles")
        numeric = np.column_stack([floats(index) for index in range(5, 12)])
        if not np.isfinite(numeric).all():
            raise RefineParquetError("point contains non-finite numeric fields")
        if np.any(np.abs(numeric[:,1]) > 180) or np.any(np.abs(numeric[:,2]) > 90):
            raise RefineParquetError("point longitude/latitude is outside EPSG:4326")
        for row in rows:
            if row[12] == "kept":
                if row[13] is None or row[14] is not None or row[15] is not None:
                    raise RefineParquetError("kept point identity fields are invalid")
            elif row[13] is not None or not isinstance(row[14], str) or not row[14] or isinstance(row[15], bool) or not isinstance(row[15], int):
                raise RefineParquetError("injected point identity fields are invalid")
        kept = np.fromiter((row[12] == "kept" for row in rows), dtype=np.bool_, count=size)
        return refinement_subtrack_from_columns(
            vehicle_id=str(rows[0][2]), subtrack_index=int(rows[0][3]), source_id=source_table,
            xy=np.column_stack((floats(8), floats(9))),
            lonlat=np.column_stack((floats(6), floats(7))), timestamp=floats(5),
            heading=floats(10), speed=floats(11), kind=np.where(kept, "p", "i"),
            source_row_key=np.asarray([int(row[13]) if row[13] is not None else 0 for row in rows], dtype=np.int64),
            refine_point_id=integers(1),
            generated_reason=np.asarray([row[14] or "" for row in rows]),
            after_sequence=np.asarray([-1 if row[15] is None else int(row[15]) for row in rows], dtype=np.int64),
        )
