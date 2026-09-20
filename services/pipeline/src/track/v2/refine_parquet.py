"""Export one running Refine Run from PostgreSQL into staged Parquet v1."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any
from uuid import UUID

import duckdb

from track.v2.artifact_parquet import build_descriptor, validate_published
from track.v2.artifact_publication import StagingDataset, _validate_handle
from track.v2.candidate_patches import (
    CROSS_TRACK_OVERFLOW_PAIR_LIMIT, CrossTrackCapacityError,
)
from track.v2.config import PostgresConfig
from track.v2.db.artifact_registry import ArtifactDescriptor
from track.v2.db.source_table import SourceTableContract, validate_source_table
from track.v2.models import PointRef, RefinedPointRef, RuntimeConfig, TrajectoryPoint, TurnEvent
from track.v2.projection import spatial_context_from_values

if TYPE_CHECKING:
    from .cross_track import _CrossTrackSegment


class RefineParquetError(RuntimeError):
    """The authoritative Refine result cannot be exported safely."""


class RefineDatasetReader:
    """Own a validated, read-only DuckDB view of one Refine v1 dataset."""

    def __init__(
        self, publication_root: Path | str, descriptor: ArtifactDescriptor, state: str,
        runtime: RuntimeConfig, *, verify_sha256: bool = False,
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
        self._connection: duckdb.DuckDBPyConnection | None = None

    @property
    def descriptor(self) -> ArtifactDescriptor:
        """Return the validated immutable descriptor for provenance checks."""
        return self._descriptor

    def __enter__(self) -> RefineDatasetReader:
        if self._connection is not None:
            raise RefineParquetError("dataset reader is already open")
        run_id = self._descriptor.refine_id
        temp = self._root.parent / "duckdb" / str(run_id)
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
            self._connection.close()
            self._connection = None
            raise
        return self

    def __exit__(self, *_args: object) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def iter_subtrack_batches(
        self, vehicle_id: str, subtrack_index: int, *, batch_size: int = 4096,
    ) -> Iterator[tuple[tuple[Any, ...], ...]]:
        """Yield bounded row batches ordered across kept and injected points."""
        if self._connection is None:
            raise RefineParquetError("dataset reader is not open")
        if not isinstance(vehicle_id, str) or not vehicle_id or isinstance(subtrack_index, bool):
            raise RefineParquetError("vehicle_id and subtrack_index are required")
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

    def iter_tracks(
        self, source_table: str, *, batch_size: int = 4096,
    ) -> Iterator[list[TrajectoryPoint]]:
        """Yield validated Detect input, retaining at most one track and one batch."""
        connection = self._require_open()
        if not isinstance(source_table, str) or not source_table.strip():
            raise RefineParquetError("source_table is required")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise RefineParquetError("batch_size is invalid")
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
        current_key: tuple[str, int] | None = None
        track: list[TrajectoryPoint] = []
        while rows := result.fetchmany(batch_size):
            for row in rows:
                point, key = self._decode_row(row, source_table, len(track), current_key)
                if current_key is not None and key != current_key:
                    yield track
                    track = []
                    point, key = self._decode_row(row, source_table, 0, None)
                current_key = key
                track.append(point)
        if track:
            yield track

    def iter_turn_candidates(
        self, source_table: str, cfg: Any, *, batch_size: int = 4096,
    ) -> Iterator[TurnEvent]:
        """Yield SQL-filtered turn candidates without materializing all points."""
        connection = self._require_open()
        if not isinstance(source_table, str) or not source_table.strip():
            raise RefineParquetError("source_table is required")
        w = cfg.turn_window
        if isinstance(w, bool) or not isinstance(w, int) or w < 1:
            raise RefineParquetError("turn_window must be a positive integer")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise RefineParquetError("batch_size is invalid")
        thresholds = (cfg.min_turn_deg, cfg.max_turn_deg, cfg.min_leg_m,
                      cfg.min_movement_speed_mps)
        if (not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and value >= 0 for value in thresholds)
                or cfg.min_turn_deg > cfg.max_turn_deg):
            raise RefineParquetError("turn thresholds are invalid")
        frame_after = w - 1
        query = f"""
          WITH windows AS (
            SELECT *,
              row_number() OVER track AS position,
              count(*) OVER (PARTITION BY vehicle_id, subtrack_index) AS track_size,
              lag(x, {w}) OVER track AS before_x,
              lag(y, {w}) OVER track AS before_y,
              lag(timestamp, {w}) OVER track AS before_time,
              lead(x, {w}) OVER track AS after_x,
              lead(y, {w}) OVER track AS after_y,
              lead(timestamp, {w}) OVER track AS after_time,
              atan2(sum(sin(radians(heading))) OVER
                (PARTITION BY vehicle_id, subtrack_index ORDER BY refined_sequence_in_subtrack
                 ROWS BETWEEN {w} PRECEDING AND 1 PRECEDING),
                    sum(cos(radians(heading))) OVER
                (PARTITION BY vehicle_id, subtrack_index ORDER BY refined_sequence_in_subtrack
                 ROWS BETWEEN {w} PRECEDING AND 1 PRECEDING)) * 180.0 / pi() AS in_raw,
              atan2(sum(sin(radians(heading))) OVER
                (PARTITION BY vehicle_id, subtrack_index ORDER BY refined_sequence_in_subtrack
                 ROWS BETWEEN CURRENT ROW AND {frame_after} FOLLOWING),
                    sum(cos(radians(heading))) OVER
                (PARTITION BY vehicle_id, subtrack_index ORDER BY refined_sequence_in_subtrack
                 ROWS BETWEEN CURRENT ROW AND {frame_after} FOLLOWING)) * 180.0 / pi() AS out_raw
            FROM refine_points
            WINDOW track AS (PARTITION BY vehicle_id, subtrack_index
                             ORDER BY refined_sequence_in_subtrack)
          ), measures AS (
            SELECT *, sqrt(pow(x-before_x, 2)+pow(y-before_y, 2)) AS in_leg,
              sqrt(pow(after_x-x, 2)+pow(after_y-y, 2)) AS out_leg,
              after_time-before_time AS elapsed,
              (in_raw + 360.0) % 360.0 AS in_bearing,
              (out_raw + 360.0) % 360.0 AS out_bearing
            FROM windows WHERE position > {w} AND position + {w} <= track_size
          ), turns AS (
            SELECT *, (((out_bearing-in_bearing+180.0) % 360.0 + 360.0)
                       % 360.0)-180.0 AS turn_deg
            FROM measures WHERE elapsed > 0 AND in_leg >= ? AND out_leg >= ?
              AND (in_leg+out_leg)/elapsed >= ?
          )
          SELECT refine_id, point_id, vehicle_id, subtrack_index,
                 refined_sequence_in_subtrack, timestamp, lon, lat, x, y,
                 point_kind, source_row_key, generated_reason,
                 after_sequence_in_subtrack, in_bearing, out_bearing, turn_deg
          FROM turns WHERE abs(turn_deg) BETWEEN ? AND ?
          ORDER BY vehicle_id, subtrack_index, refined_sequence_in_subtrack,
                   point_kind, point_id
        """
        result = connection.execute(query, [cfg.min_leg_m, cfg.min_leg_m,
                                            cfg.min_movement_speed_mps,
                                            cfg.min_turn_deg, cfg.max_turn_deg])
        event_index = 0
        while rows := result.fetchmany(batch_size):
            for row in rows:
                yield self._decode_turn_row(row, source_table, event_index)
                event_index += 1

    def iter_cross_track_segments(
        self, source_table: str, cfg: Any, *, batch_size: int = 4096,
    ) -> Iterator[_CrossTrackSegment]:
        """Yield straight adjacent segments prepared by spillable DuckDB SQL."""
        connection, threshold = self._validate_cross_track_request(
            source_table, cfg, batch_size,
        )
        self._validate_cross_track_source(connection)
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
            for row in rows:
                yield self._decode_cross_track_segment(row, source_table)

    def iter_cross_track_pair_batches(
        self, source_table: str, cfg: Any, *, batch_size: int = 4096,
    ) -> Iterator[tuple[tuple[_CrossTrackSegment, _CrossTrackSegment], ...]]:
        """Yield stable broad-phase pairs; DuckDB owns expansion and deduplication."""
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
        self._validate_cross_track_source(connection)
        relation = self._cross_track_pair_relation()
        parameters = [threshold, float(cell_m), float(cell_m), float(cell_m),
                      float(cell_m), max_cells]
        estimate = connection.execute(
            relation + " SELECT count(*) * (SELECT count(*) FROM spans) FROM spans WHERE overflow",
            parameters,
        ).fetchone()
        if estimate is None or estimate[0] is None:
            raise RefineParquetError("cross-track overflow estimate failed")
        if int(estimate[0]) > CROSS_TRACK_OVERFLOW_PAIR_LIMIT:
            raise CrossTrackCapacityError(
                "global cross-track overflow fallback spans "
                f"{int(estimate[0]):,} segment pairs; budget is "
                f"{CROSS_TRACK_OVERFLOW_PAIR_LIMIT:,}"
            )
        query = relation + """
          , memberships AS (
            SELECT segment_id, vehicle_rank, min_cell_x, min_cell_y, cell_x, cell_y
            FROM spans,
              LATERAL range(min_cell_x, max_cell_x + 1) cells_x(cell_x),
              LATERAL range(min_cell_y, max_cell_y + 1) cells_y(cell_y)
            WHERE NOT overflow
          ), grid_pairs AS (
            SELECT a.segment_id AS a_id, b.segment_id AS b_id
            FROM memberships a JOIN memberships b
              ON a.cell_x = b.cell_x AND a.cell_y = b.cell_y
             AND a.segment_id < b.segment_id AND a.vehicle_rank != b.vehicle_rank
             AND greatest(a.min_cell_x, b.min_cell_x) = a.cell_x
             AND greatest(a.min_cell_y, b.min_cell_y) = a.cell_y
          ), overflow_pairs AS (
            SELECT a.segment_id AS a_id, b.segment_id AS b_id
            FROM spans a JOIN spans b ON a.segment_id < b.segment_id
             AND a.vehicle_rank != b.vehicle_rank AND (a.overflow OR b.overflow)
          ), pairs AS (
            SELECT * FROM grid_pairs UNION ALL SELECT * FROM overflow_pairs
          )
          SELECT a.*, b.* FROM pairs
          JOIN straight a ON a.segment_id = pairs.a_id
          JOIN straight b ON b.segment_id = pairs.b_id
          ORDER BY pairs.a_id, pairs.b_id
        """
        result = connection.execute(query, parameters)
        while rows := result.fetchmany(batch_size):
            yield tuple((self._decode_cross_track_segment(row[:23], source_table),
                         self._decode_cross_track_segment(row[23:], source_table))
                        for row in rows)

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

    def _validate_cross_track_source(self, connection: duckdb.DuckDBPyConnection) -> None:
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
            raise RefineParquetError("cross-track source contains corrupt identity or sequence data")

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
                   vehicle_rank, track_rank, segment_index, end_x, end_y
            FROM deltas WHERE abs(heading_delta) <= ?
          )
        """ + select_sql

    @classmethod
    def _cross_track_pair_relation(cls) -> str:
        return cls._cross_track_segment_relation("""
          , spans AS (
            SELECT *, floor(least(x, end_x) / ?)::BIGINT AS min_cell_x,
                   floor(greatest(x, end_x) / ?)::BIGINT AS max_cell_x,
                   floor(least(y, end_y) / ?)::BIGINT AS min_cell_y,
                   floor(greatest(y, end_y) / ?)::BIGINT AS max_cell_y,
                   ((max_cell_x-min_cell_x+1) * (max_cell_y-min_cell_y+1)) > ? AS overflow
            FROM straight
          )
        """)

    def _decode_cross_track_segment(
        self, row: tuple[Any, ...], source_table: str,
    ) -> _CrossTrackSegment:
        from .cross_track import _CrossTrackSegment
        # segment_id prefixes the 22 columns used by C07b.
        data = row[1:]
        point, _ = self._decode_row(
            data[:17], source_table, int(data[19]),
            (str(data[2]), int(data[3])),
        )
        coordinates = tuple(float(value) for value in (data[8], data[9], data[20], data[21]))
        if not all(math.isfinite(value) for value in coordinates):
            raise RefineParquetError("cross-track segment contains non-finite coordinates")
        return _CrossTrackSegment(
            vehicle_id=point.vehicle_id, vehicle_rank=int(data[17]),
            track_rank=int(data[18]), segment_index=int(data[19]), start=point,
            x1=coordinates[0], y1=coordinates[1], x2=coordinates[2],
            y2=coordinates[3], heading=point.heading, timestamp=point.timestamp,
        )

    def _decode_turn_row(
        self, row: tuple[Any, ...], source_table: str, event_index: int,
    ) -> TurnEvent:
        if len(row) != 17 or row[0] != self._descriptor.refine_id:
            raise RefineParquetError("turn row owner differs from descriptor owner")
        point_row = row[:10] + (0.0, 0.0) + row[10:14] + (1,)
        point, _ = self._decode_row(
            point_row, source_table, int(row[4]), (str(row[2]), int(row[3])),
        )
        values = tuple(float(value) for value in row[5:10] + row[14:17])
        if not all(math.isfinite(value) for value in values):
            raise RefineParquetError("turn row contains non-finite numeric fields")
        timestamp, lon, lat, x, y, entry, exit_, turn = values
        return TurnEvent(
            event_calculate_index=event_index, vehicle_id=point.vehicle_id,
            timestamp=timestamp, lon=lon, lat=lat, x=x, y=y,
            entry_bearing=entry, exit_bearing=exit_, turn_deg=turn,
            score=abs(turn), source="turn", point_ref=point.point_ref,
            refined_point_ref=point.refined_point_ref,
            source_ids=(point.point_ref.source_id,),
        )

    def _require_open(self) -> duckdb.DuckDBPyConnection:
        if self._connection is None:
            raise RefineParquetError("dataset reader is not open")
        return self._connection

    def _decode_row(
        self, row: tuple[Any, ...], source_table: str, expected_sequence: int,
        current_key: tuple[str, int] | None,
    ) -> tuple[TrajectoryPoint, tuple[str, int]]:
        if len(row) != 17 or row[0] != self._descriptor.refine_id:
            raise RefineParquetError("row owner differs from descriptor owner")
        kind, point_id, vehicle_id = row[12], row[1], row[2]
        if kind not in ("kept", "injected") or not isinstance(vehicle_id, str) or not vehicle_id:
            raise RefineParquetError("row identity is invalid")
        if isinstance(point_id, bool) or not isinstance(point_id, int):
            raise RefineParquetError("point identity is invalid")
        subtrack_index, sequence = row[3], row[4]
        if (isinstance(subtrack_index, bool) or not isinstance(subtrack_index, int)
                or subtrack_index < 0 or isinstance(sequence, bool)
                or not isinstance(sequence, int) or sequence < 0):
            raise RefineParquetError("subtrack identity is invalid")
        if row[16] != 1:
            raise RefineParquetError("subtrack contains multiple vehicles")
        key = (vehicle_id, subtrack_index)
        if current_key == key and sequence != expected_sequence:
            raise RefineParquetError("subtrack sequence is not contiguous from zero")
        if current_key != key and sequence != 0:
            raise RefineParquetError("subtrack sequence does not start at zero")
        try:
            values = tuple(float(row[index]) for index in range(5, 12))
        except (TypeError, ValueError) as exc:
            raise RefineParquetError("point numeric fields are invalid") from exc
        timestamp, lon, lat, x, y, heading, speed = values
        if not all(math.isfinite(value) for value in values):
            raise RefineParquetError("point contains non-finite numeric fields")
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            raise RefineParquetError("point longitude/latitude is outside EPSG:4326")
        source_row_key, reason, after_sequence = row[13], row[14], row[15]
        if kind == "kept":
            if not isinstance(source_row_key, str) or not source_row_key or reason is not None or after_sequence is not None:
                raise RefineParquetError("kept point identity fields are invalid")
            point_ref = PointRef(source_table, source_row_key, True)
        else:
            if source_row_key is not None or not isinstance(reason, str) or not reason or isinstance(after_sequence, bool) or not isinstance(after_sequence, int):
                raise RefineParquetError("injected point identity fields are invalid")
            point_ref = PointRef("public.injected_points", str(point_id), False,
                                 generated_reason=reason)
        return TrajectoryPoint(
            point_ref=point_ref, vehicle_id=vehicle_id, timestamp=timestamp,
            lon=lon, lat=lat, heading=heading, speed=speed, extra={}, x=x, y=y,
            refined_point_ref=RefinedPointRef(kind, point_id, subtrack_index, sequence),
        ), key


@dataclass(frozen=True, slots=True)
class ParquetColumn:
    name: str
    duckdb_type: str
    # DuckDB 1.5.5 writes flat COPY output as physically OPTIONAL Parquet fields.
    nullable: bool = True
    required: bool = True


_COMMON = (
    ParquetColumn("refine_id", "UUID"), ParquetColumn("point_id", "BIGINT"),
    ParquetColumn("vehicle_id", "VARCHAR"), ParquetColumn("subtrack_index", "INTEGER"),
    ParquetColumn("refined_sequence_in_subtrack", "BIGINT"),
    ParquetColumn("timestamp", "DOUBLE"), ParquetColumn("lon", "DOUBLE"),
    ParquetColumn("lat", "DOUBLE"), ParquetColumn("x", "DOUBLE"),
    ParquetColumn("y", "DOUBLE"), ParquetColumn("heading", "DOUBLE"),
    ParquetColumn("speed", "DOUBLE"),
)
REFINE_PARQUET_V1 = {
    "kept_points": (*_COMMON, ParquetColumn("source_row_key", "VARCHAR")),
    "injected_points": (
        *_COMMON, ParquetColumn("generated_reason", "VARCHAR"),
        ParquetColumn("after_sequence_in_subtrack", "INTEGER"),
    ),
}


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _quote_identifier(value: str) -> str:
    if not value or "\x00" in value:
        raise RefineParquetError("current PostgreSQL schema is unavailable")
    return '"' + value.replace('"', '""') + '"'


def _run_metadata(conn: Any, refine_id: UUID) -> tuple[str, int, str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_table, source_srid, processing_srid, projection_config, status, "
            "current_schema() "
            "FROM refine_runs WHERE id = %s",
            (refine_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise RefineParquetError(f"unknown Refine Run: {refine_id}")
    if str(row[4]) != "running":
        raise RefineParquetError("Refine Run must be running during staging export")
    if not str(row[0]).strip():
        raise RefineParquetError("Refine Run source metadata is required")
    if not isinstance(row[5], str):
        raise RefineParquetError("current PostgreSQL schema is unavailable")
    result_schema = _quote_identifier(row[5])
    try:
        spatial_context = spatial_context_from_values(row[1], row[2], row[3])
    except (TypeError, ValueError) as exc:
        raise RefineParquetError("Refine Run processing_srid metadata is invalid") from exc
    return str(row[0]), spatial_context.processing_srid, result_schema


def _postgres_query(
    refine_id: UUID, contract: SourceTableContract, srid: int, result_schema: str,
) -> str:
    run = _literal(str(refine_id))
    return f"""
WITH points AS (
 SELECT k.refine_id, k.id AS point_id, k.vehicle_id, k.subtrack_index,
        k.sequence_in_subtrack AS anchor_sequence, 0 AS point_kind,
        s.timestamp, s.lon, s.lat,
        ST_X(ST_Transform(ST_SetSRID(ST_MakePoint(s.lon, s.lat), 4326), {srid})) AS x,
        ST_Y(ST_Transform(ST_SetSRID(ST_MakePoint(s.lon, s.lat), 4326), {srid})) AS y,
        s.heading, s.speed, k.source_row_key, NULL::text AS generated_reason,
        NULL::integer AS after_sequence_in_subtrack
 FROM {result_schema}.kept_trajectory_points k JOIN {contract.quoted_table} s
   ON s.id = k.source_row_key::{contract.key_sql_type}
 WHERE k.refine_id = {run}::uuid
 UNION ALL
 SELECT i.refine_id, i.id, i.vehicle_id, i.subtrack_index,
        i.after_sequence_in_subtrack, 1, i.timestamp, i.lon, i.lat, i.x, i.y,
        i.heading, i.speed, NULL::text, i.generated_reason,
        i.after_sequence_in_subtrack
 FROM {result_schema}.injected_points i WHERE i.refine_id = {run}::uuid
), sequenced AS (
 SELECT *, row_number() OVER (
   PARTITION BY subtrack_index
   ORDER BY anchor_sequence, point_kind, point_id
 ) - 1 AS refined_sequence_in_subtrack
 FROM points
)
SELECT * FROM sequenced""".strip()


def _relation(postgres_sql: str, table: str) -> str:
    columns = REFINE_PARQUET_V1[table]
    projection = ", ".join(f"{column.name}::{column.duckdb_type} AS {column.name}" for column in columns)
    point_kind = 0 if table == "kept_points" else 1
    return (
        f"SELECT {projection} FROM postgres_query('authoritative_pg', {_literal(postgres_sql)}) "
        f"WHERE point_kind = {point_kind} "
        "ORDER BY subtrack_index, refined_sequence_in_subtrack, point_id"
    )


def _open_duckdb(runtime: RuntimeConfig, temp_directory: Path) -> duckdb.DuckDBPyConnection:
    extension = Path(os.environ.get("DUCKDB_POSTGRES_EXTENSION_PATH", ""))
    if not extension.is_absolute() or not extension.is_file():
        raise RefineParquetError("DUCKDB_POSTGRES_EXTENSION_PATH must name a bundled absolute file")
    temp_directory.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(database=":memory:", config={
        "autoinstall_known_extensions": "false", "autoload_known_extensions": "false",
        "memory_limit": runtime.duckdb_memory_limit, "threads": str(runtime.duckdb_threads),
        "temp_directory": str(temp_directory),
        "max_temp_directory_size": runtime.duckdb_max_temp_directory_size,
    })
    connection.execute(f"LOAD {_literal(str(extension))}")
    return connection


def _copy_table(
    connection: duckdb.DuckDBPyConnection, relation: str, output: Path,
    table: str, runtime: RuntimeConfig,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=False)
    options = ["FORMAT PARQUET"]
    if runtime.parquet_row_group_size_rows:
        options.append(f"ROW_GROUP_SIZE {runtime.parquet_row_group_size_rows}")
    connection.execute(f"COPY ({relation}) TO {_literal(str(output))} ({', '.join(options)})")
    null_predicate = " OR ".join(f"{column.name} IS NULL" for column in REFINE_PARQUET_V1[table])
    if connection.execute(
        f"SELECT count(*) FROM read_parquet({_literal(str(output))}) WHERE {null_predicate}"
    ).fetchone()[0]:
        raise RefineParquetError(f"{table} contains null output fields")


def _validate_schema(descriptor: ArtifactDescriptor) -> None:
    tables = {table["name"]: table for table in json.loads(descriptor.manifest_json)["tables"]}
    if set(tables) != set(REFINE_PARQUET_V1):
        raise RefineParquetError("Refine descriptor has unexpected logical tables")
    for name, columns in REFINE_PARQUET_V1.items():
        expected = [
            {"name": column.name, "type": column.duckdb_type, "nullable": column.nullable}
            for column in columns
        ]
        if tables[name]["schema"] != expected:
            raise RefineParquetError(f"{name} Parquet schema differs from v1")


def export_refine_parquet(
    conn: Any, postgres: PostgresConfig, handle: StagingDataset,
    runtime: RuntimeConfig, source_fingerprint: dict[str, object],
) -> ArtifactDescriptor:
    """Write and inspect staging only; never publish, commit, rollback, or finish.

    The Refine rows must already be committed and visible while the Run remains
    ``running``: postgres_scanner uses its own read-only PostgreSQL connection.
    C05b owns that pre-export commit and the later publication transaction.
    """
    _validate_handle(handle)
    if handle.stage != "refine":
        raise RefineParquetError("staging dataset must belong to the refine stage")
    source_table, srid, result_schema = _run_metadata(conn, handle.run_id)
    contract = validate_source_table(conn, source_table)
    postgres_sql = _postgres_query(handle.run_id, contract, srid, result_schema)
    data_root = Path(os.environ.get("DATA_ROOT", "/data"))
    outputs = {
        name: f"{name}/part-00000.parquet" for name in REFINE_PARQUET_V1
    }
    # C05a deliberately writes one file per logical table. Target file sizing
    # remains zero/engine-default until the benchmark introduces partitioning.
    with _open_duckdb(runtime, data_root / "duckdb" / str(handle.run_id)) as connection:
        connection.execute(
            f"ATTACH {_literal(postgres.dsn())} AS authoritative_pg "
            "(TYPE postgres, READ_ONLY)"
        )
        for name, relative in outputs.items():
            _copy_table(
                connection, _relation(postgres_sql, name),
                handle.path / relative, name, runtime,
            )
    descriptor = build_descriptor(
        handle, {name: [relative] for name, relative in outputs.items()}, source_fingerprint,
    )
    _validate_schema(descriptor)
    return descriptor
