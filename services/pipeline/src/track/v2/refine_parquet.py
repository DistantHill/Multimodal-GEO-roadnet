"""Export one running Refine Run from PostgreSQL into staged Parquet v1."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import shutil
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import duckdb

from track.v2.artifact_parquet import build_descriptor, validate_published
from track.v2.artifact_publication import StagingDataset, _validate_handle
from track.v2.config import PostgresConfig
from track.v2.db.artifact_registry import ArtifactDescriptor
from track.v2.db.queries import SpatialWindow
from track.v2.db.source_table import SourceTableContract, validate_source_table
from track.v2._geometry import _angle_delta_deg, _circular_mean_deg
from track.v2.models import PointRef, RefinedPointRef, RuntimeConfig, TrajectoryPoint, TurnEvent
from track.v2.projection import spatial_context_from_values

if TYPE_CHECKING:
    from .cross_track import _CrossTrackSegment


class RefineParquetError(RuntimeError):
    """The authoritative Refine result cannot be exported safely."""


@dataclass(frozen=True, slots=True)
class TopologyTrackSelection:
    window_indices: tuple[int, ...]
    track_key: str
    points: list[TrajectoryPoint]


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
    ) -> Iterator[list[TrajectoryPoint]]:
        """Yield validated Detect input, retaining at most one track and one batch."""
        connection = self._require_open()
        if not isinstance(source_table, str) or not source_table.strip():
            raise RefineParquetError("source_table is required")
        batch_size = self._runtime.parquet_read_batch_size if batch_size is None else batch_size
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
            points: list[TrajectoryPoint] = []
            for rows in iter(lambda: result.fetchmany(batch_size), []):
                for row in rows:
                    key = (int(row[17]), str(row[2]), int(row[3]))
                    if current is not None and key != current:
                        yield TopologyTrackSelection(
                            canonical[current[0]][1], f"{current[1]}\0{current[2]}", points,
                        )
                        points = []
                    point, _ = self._decode_row(
                        row[:17], source_table, len(points),
                        None if not points else (key[1], key[2]),
                    )
                    current = key
                    points.append(point)
            if current is not None:
                yield TopologyTrackSelection(
                    canonical[current[0]][1], f"{current[1]}\0{current[2]}", points,
                )
        finally:
            connection.execute(f"DROP TABLE IF EXISTS {table}")

    def iter_turn_candidates(
        self, source_table: str, cfg: Any, *, batch_size: int | None = None,
    ) -> Iterator[TurnEvent]:
        """Yield SQL-filtered turn candidates without materializing all points."""
        connection = self._require_open()
        if not isinstance(source_table, str) or not source_table.strip():
            raise RefineParquetError("source_table is required")
        w = cfg.turn_window
        if isinstance(w, bool) or not isinstance(w, int) or w < 1:
            raise RefineParquetError("turn_window must be a positive integer")
        batch_size = self._runtime.parquet_read_batch_size if batch_size is None else batch_size
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise RefineParquetError("batch_size is invalid")
        thresholds = (cfg.min_turn_deg, cfg.max_turn_deg, cfg.min_leg_m,
                      cfg.min_movement_speed_mps)
        if (not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and value >= 0 for value in thresholds)
                or cfg.min_turn_deg > cfg.max_turn_deg):
            raise RefineParquetError("turn thresholds are invalid")
        frame_after = w - 1
        tolerance = 1e-12
        lower = math.nextafter(cfg.min_turn_deg - tolerance, -math.inf)
        upper = math.nextafter(cfg.max_turn_deg + tolerance, math.inf)
        unit_roundoff = 2.0 ** -53
        gamma = (w * unit_roundoff) / (1.0 - w * unit_roundoff)
        query = f"""
          WITH window_sums AS (
            SELECT *,
              row_number() OVER track AS position,
              count(*) OVER (PARTITION BY vehicle_id, subtrack_index) AS track_size,
              lag(x, {w}) OVER track AS before_x,
              lag(y, {w}) OVER track AS before_y,
              lag(timestamp, {w}) OVER track AS before_time,
              lead(x, {w}) OVER track AS after_x,
              lead(y, {w}) OVER track AS after_y,
              lead(timestamp, {w}) OVER track AS after_time,
              sum(sin(radians(heading))) OVER
                (PARTITION BY vehicle_id, subtrack_index ORDER BY refined_sequence_in_subtrack
                 ROWS BETWEEN {w} PRECEDING AND 1 PRECEDING) AS in_sin,
              sum(cos(radians(heading))) OVER
                (PARTITION BY vehicle_id, subtrack_index ORDER BY refined_sequence_in_subtrack
                 ROWS BETWEEN {w} PRECEDING AND 1 PRECEDING) AS in_cos,
              sum(abs(sin(radians(heading)))) OVER
                (PARTITION BY vehicle_id, subtrack_index ORDER BY refined_sequence_in_subtrack
                 ROWS BETWEEN {w} PRECEDING AND 1 PRECEDING) AS in_sin_abs,
              sum(abs(cos(radians(heading)))) OVER
                (PARTITION BY vehicle_id, subtrack_index ORDER BY refined_sequence_in_subtrack
                 ROWS BETWEEN {w} PRECEDING AND 1 PRECEDING) AS in_cos_abs,
              list(heading) OVER
                (PARTITION BY vehicle_id, subtrack_index ORDER BY refined_sequence_in_subtrack
                 ROWS BETWEEN {w} PRECEDING AND 1 PRECEDING) AS in_headings,
              sum(sin(radians(heading))) OVER
                (PARTITION BY vehicle_id, subtrack_index ORDER BY refined_sequence_in_subtrack
                 ROWS BETWEEN CURRENT ROW AND {frame_after} FOLLOWING) AS out_sin,
              sum(cos(radians(heading))) OVER
                (PARTITION BY vehicle_id, subtrack_index ORDER BY refined_sequence_in_subtrack
                 ROWS BETWEEN CURRENT ROW AND {frame_after} FOLLOWING) AS out_cos,
              sum(abs(sin(radians(heading)))) OVER
                (PARTITION BY vehicle_id, subtrack_index ORDER BY refined_sequence_in_subtrack
                 ROWS BETWEEN CURRENT ROW AND {frame_after} FOLLOWING) AS out_sin_abs,
              sum(abs(cos(radians(heading)))) OVER
                (PARTITION BY vehicle_id, subtrack_index ORDER BY refined_sequence_in_subtrack
                 ROWS BETWEEN CURRENT ROW AND {frame_after} FOLLOWING) AS out_cos_abs,
              list(heading) OVER
                (PARTITION BY vehicle_id, subtrack_index ORDER BY refined_sequence_in_subtrack
                 ROWS BETWEEN CURRENT ROW AND {frame_after} FOLLOWING) AS out_headings
            FROM refine_points
            WINDOW track AS (PARTITION BY vehicle_id, subtrack_index
                             ORDER BY refined_sequence_in_subtrack)
          ), windows AS (
            SELECT *, atan2(in_sin, in_cos) * 180.0 / pi() AS in_raw,
              atan2(out_sin, out_cos) * 180.0 / pi() AS out_raw,
              sqrt(in_sin*in_sin + in_cos*in_cos) AS in_resultant,
              in_sin_abs + in_cos_abs AS in_abs_sum,
              sqrt(out_sin*out_sin + out_cos*out_cos) AS out_resultant,
              out_sin_abs + out_cos_abs AS out_abs_sum
            FROM window_sums
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
                 after_sequence_in_subtrack, in_bearing, out_bearing, turn_deg,
                 in_headings, out_headings, in_resultant, in_abs_sum,
                 out_resultant, out_abs_sum
          FROM turns WHERE abs(turn_deg) BETWEEN ? AND ?
             OR in_resultant <= ? * in_abs_sum
             OR out_resultant <= ? * out_abs_sum
          ORDER BY vehicle_id, subtrack_index, refined_sequence_in_subtrack,
                   point_kind, point_id
        """
        result = connection.execute(query, [cfg.min_leg_m, cfg.min_leg_m,
                                            cfg.min_movement_speed_mps,
                                            lower, upper, gamma, gamma])
        event_index = 0
        while rows := result.fetchmany(batch_size):
            for row in rows:
                sql_turn = float(row[16])
                unstable = (float(row[19]) <= gamma * float(row[20])
                            or float(row[21]) <= gamma * float(row[22]))
                boundary = (abs(abs(sql_turn) - cfg.min_turn_deg) <= tolerance
                            or abs(abs(sql_turn) - cfg.max_turn_deg) <= tolerance)
                if unstable or boundary:
                    entry = _circular_mean_deg(row[17])
                    exit_ = _circular_mean_deg(row[18])
                    turn = _angle_delta_deg(entry, exit_)
                    if not cfg.min_turn_deg <= abs(turn) <= cfg.max_turn_deg:
                        continue
                    row = row[:14] + (entry, exit_, turn) + row[17:]
                yield self._decode_turn_row(row[:17], source_table, event_index)
                event_index += 1

    def iter_cross_track_segments(
        self, source_table: str, cfg: Any, *, batch_size: int | None = None,
    ) -> Iterator[_CrossTrackSegment]:
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
            for row in rows:
                yield self._decode_cross_track_segment(row, source_table)

    def iter_cross_track_pair_batches(
        self, source_table: str, cfg: Any, *, batch_size: int | None = None,
    ) -> Iterator[tuple[tuple[_CrossTrackSegment, _CrossTrackSegment], ...]]:
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
           AND a.vehicle_rank != b.vehicle_rank
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
                yield tuple((self._decode_cross_track_segment(row[:23], source_table),
                             self._decode_cross_track_segment(row[23:], source_table))
                            for row in rows)
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
        s.heading, s.speed, k.source_row_key::text AS source_row_key, NULL::text AS generated_reason,
        NULL::integer AS after_sequence_in_subtrack
 FROM {result_schema}.kept_trajectory_points k JOIN {contract.quoted_table} s
   ON s.id = k.source_row_key
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


def _open_duckdb(
    runtime: RuntimeConfig,
    temp_directory: Path,
    postgres_extension_path: Path,
) -> duckdb.DuckDBPyConnection:
    extension = postgres_extension_path
    if not extension.is_absolute() or not extension.is_file():
        raise RefineParquetError(
            "paths.duckdb_postgres_extension_path must name a bundled absolute file"
        )
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
    *, data_root: Path, postgres_extension_path: Path,
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
    outputs = {
        name: f"{name}/part-00000.parquet" for name in REFINE_PARQUET_V1
    }
    # C05a deliberately writes one file per logical table. Target file sizing
    # remains zero/engine-default until the benchmark introduces partitioning.
    with _open_duckdb(
        runtime,
        data_root / "duckdb" / str(handle.run_id),
        postgres_extension_path,
    ) as connection:
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
