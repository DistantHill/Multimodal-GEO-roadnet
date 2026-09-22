"""DuckDB-managed normalized relation for transient Detect turn events."""
from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from itertools import islice
import math
from numbers import Integral
from pathlib import Path
from uuid import uuid4

import duckdb
import numpy as np

from .models import PointRef, RefinedPointRef, RuntimeConfig, TurnEvent


@dataclass(frozen=True, slots=True)
class TurnCoordinate:
    event_calculate_index: int
    x: float
    y: float


def _nullable_integer(values):
    values = list(values)
    if any(value is not None and (
        isinstance(value, (bool, np.bool_)) or not isinstance(value, Integral)
        or not -(2**63) <= int(value) < 2**63
    ) for value in values):
        raise TypeError("nullable integer must fit signed int64")
    return (
        np.asarray([0 if value is None else value for value in values], dtype=np.int64),
        np.asarray([value is None for value in values], dtype=np.bool_),
    )


def _nullable_float(values):
    values = list(values)
    nulls = np.asarray([value is None for value in values], dtype=np.bool_)
    nans = np.asarray([
        value is not None and math.isnan(float(value)) for value in values
    ], dtype=np.bool_)
    data = np.asarray([
        0.0 if value is None or math.isnan(float(value)) else value for value in values
    ], dtype=np.float64)
    return data, nulls, nans


def _nullable_string(values):
    values = list(values)
    data = np.asarray([
        "" if value is None else str(value).lower() if isinstance(value, bool) else str(value)
        for value in values
    ], dtype=np.str_)
    return data, np.asarray([value is None for value in values], dtype=np.bool_)


class TurnEventRelation(Sequence[TurnEvent]):
    """Append once, validate, then repeatedly stream turn events."""

    def __init__(
        self, runtime: RuntimeConfig, data_root: str | Path, *, batch_size: int = 1024,
        relation_id: str | None = None,
    ) -> None:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("batch_size must be positive")
        root = Path(data_root)
        if not root.is_absolute() or root == Path(root.anchor):
            raise ValueError("data_root must be absolute and non-root")
        identity = relation_id or uuid4().hex
        if (not identity or identity in {".", ".."} or "\x00" in identity
                or Path(identity).name != identity):
            raise ValueError("relation_id must be one path component")
        parent = root / "duckdb" / "turn-event-relations"
        parent.mkdir(parents=True, exist_ok=True)
        self._temp_directory = parent / identity
        self._temp_directory.mkdir(exist_ok=False)
        self._connection: duckdb.DuckDBPyConnection | None = None
        self._batch_size, self._count, self._sealed = batch_size, 0, False
        try:
            self._connection = duckdb.connect(database=":memory:", config={
                "allow_unsigned_extensions": "false",
                "autoload_known_extensions": "false",
                "autoinstall_known_extensions": "false",
                "memory_limit": runtime.duckdb_memory_limit,
                "threads": str(runtime.duckdb_threads),
                "temp_directory": str(self._temp_directory),
                "max_temp_directory_size": runtime.duckdb_max_temp_directory_size,
            })
            self._create_schema()
        except Exception:
            self.close()
            raise

    def _create_schema(self) -> None:
        self._require_open().execute("""
            CREATE TABLE turn_events (
                ordinal BIGINT NOT NULL, event_calculate_index BIGINT,
                vehicle_id VARCHAR, timestamp DOUBLE, lon DOUBLE, lat DOUBLE,
                x DOUBLE, y DOUBLE, entry_bearing DOUBLE, exit_bearing DOUBLE,
                turn_deg DOUBLE, score DOUBLE, source VARCHAR,
                point_source_id VARCHAR, point_source_row_key VARCHAR,
                point_stable BOOLEAN, point_from_ref VARCHAR, point_to_ref VARCHAR,
                point_generated_reason VARCHAR, point_source_row_hash VARCHAR,
                refined_point_kind VARCHAR, refined_point_id BIGINT,
                refined_subtrack_index BIGINT, refined_sequence BIGINT,
                cross_track_calculate_index BIGINT, g_star_z DOUBLE
            );
            CREATE TABLE source_ids (
                event_ordinal BIGINT NOT NULL, source_ordinal BIGINT, source_id VARCHAR
            )
        """)

    def append(self, event: TurnEvent) -> None:
        if self._sealed:
            raise RuntimeError("turn event relation is sealed")
        connection, ordinal = self._require_open(), self._count
        point, refined = event.point_ref, event.refined_point_ref
        connection.execute("BEGIN")
        try:
            connection.execute(
                "INSERT INTO turn_events VALUES (" + ",".join("?" for _ in range(26)) + ")",
                (
                    ordinal, event.event_calculate_index, event.vehicle_id, event.timestamp,
                    event.lon, event.lat, event.x, event.y, event.entry_bearing,
                    event.exit_bearing, event.turn_deg, event.score, event.source,
                    *(self._point_values(point)), *(self._refined_values(refined)),
                    event.cross_track_calculate_index, event.g_star_z,
                ),
            )
            rows = ((ordinal, index, value) for index, value in enumerate(event.source_ids))
            while batch := tuple(islice(rows, self._batch_size)):
                connection.executemany("INSERT INTO source_ids VALUES (?,?,?)", batch)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        self._count += 1

    def extend(self, events: Iterable[TurnEvent], *, batch_size: int | None = None) -> None:
        """Commit bounded event batches; a failed batch leaves earlier batches readable."""
        if self._sealed:
            raise RuntimeError("turn event relation is sealed")
        size = self._batch_size if batch_size is None else batch_size
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise ValueError("batch_size must be positive")
        iterator = iter(events)
        while batch := tuple(islice(iterator, size)):
            self._append_batch(batch)

    append_many = extend

    def _append_batch(self, events: tuple[TurnEvent, ...]) -> None:
        connection, first_ordinal = self._require_open(), self._count
        for event in events:
            if event.g_star_z is not None and not math.isfinite(event.g_star_z):
                raise ValueError("g_star_z must be finite or None")
        points = [event.point_ref for event in events]
        refined = [event.refined_point_ref for event in events]
        columnar = {
            "ordinal": np.arange(first_ordinal, first_ordinal + len(events), dtype=np.int64),
        }
        nullable: list[str] = []
        float_names = (
            "timestamp", "lon", "lat", "x", "y", "entry_bearing", "exit_bearing",
            "turn_deg", "score", "g_star_z",
        )
        for name in float_names:
            data, nulls, nans = _nullable_float(getattr(event, name) for event in events)
            columnar[name], columnar[f"{name}__null"], columnar[f"{name}__nan"] = (
                data, nulls, nans,
            )
            nullable.append(name)
        integer_values = {
            "event_calculate_index": [event.event_calculate_index for event in events],
            "refined_point_id": [None if item is None else item.point_id for item in refined],
            "refined_subtrack_index": [
                None if item is None else item.subtrack_index for item in refined
            ],
            "refined_sequence": [
                None if item is None else item.refined_sequence_in_subtrack for item in refined
            ],
            "cross_track_calculate_index": [
                event.cross_track_calculate_index for event in events
            ],
        }
        for name, values in integer_values.items():
            columnar[name], columnar[f"{name}__null"] = _nullable_integer(values)
            nullable.append(name)
        string_values = {
            "vehicle_id": [event.vehicle_id for event in events],
            "source": [event.source for event in events],
            "point_source_id": [None if item is None else item.source_id for item in points],
            "point_source_row_key": [
                None if item is None else item.source_row_key for item in points
            ],
            "point_from_ref": [None if item is None else item.from_ref for item in points],
            "point_to_ref": [None if item is None else item.to_ref for item in points],
            "point_generated_reason": [
                None if item is None else item.generated_reason for item in points
            ],
            "point_source_row_hash": [
                None if item is None else item.source_row_hash for item in points
            ],
            "refined_point_kind": [None if item is None else item.point_kind for item in refined],
        }
        for name, values in string_values.items():
            columnar[name], columnar[f"{name}__null"] = _nullable_string(values)
            nullable.append(name)
        stable_values = [None if item is None else item.stable for item in points]
        if any(value is not None and not isinstance(value, (bool, np.bool_))
               for value in stable_values):
            raise TypeError("point_stable must be bool or None")
        columnar["point_stable"] = np.asarray(
            [False if value is None else bool(value) for value in stable_values], dtype=np.bool_,
        )
        columnar["point_stable__null"] = np.asarray(
            [value is None for value in stable_values], dtype=np.bool_,
        )
        nullable.append("point_stable")
        names = (
            "ordinal", "event_calculate_index", "vehicle_id", "timestamp", "lon", "lat",
            "x", "y", "entry_bearing", "exit_bearing", "turn_deg", "score", "source",
            "point_source_id", "point_source_row_key", "point_stable", "point_from_ref",
            "point_to_ref", "point_generated_reason", "point_source_row_hash",
            "refined_point_kind", "refined_point_id", "refined_subtrack_index",
            "refined_sequence", "cross_track_calculate_index", "g_star_z",
        )
        selects = []
        for name in names:
            if name in float_names:
                selects.append(
                    f"CASE WHEN {name}__null THEN NULL WHEN {name}__nan "
                    f"THEN 'NaN'::DOUBLE ELSE {name} END AS {name}"
                )
            elif name in nullable:
                selects.append(f"CASE WHEN {name}__null THEN NULL ELSE {name} END AS {name}")
            else:
                selects.append(name)
        registered = False
        connection.execute("BEGIN")
        try:
            connection.register("turn_event_batch", columnar)
            registered = True
            connection.execute(
                "INSERT INTO turn_events SELECT " + ",".join(selects)
                + " FROM turn_event_batch ORDER BY ordinal"
            )
            source_rows = (
                (first_ordinal + offset, source_ordinal, source_id)
                for offset, event in enumerate(events)
                for source_ordinal, source_id in enumerate(event.source_ids)
            )
            while chunk := tuple(islice(source_rows, self._batch_size)):
                source_ids = [item[2] for item in chunk]
                source_values, source_nulls = _nullable_string(source_ids)
                connection.register("turn_source_batch", {
                    "event_ordinal": np.asarray([item[0] for item in chunk], dtype=np.int64),
                    "source_ordinal": np.asarray([item[1] for item in chunk], dtype=np.int64),
                    "source_id": source_values, "source_id__null": source_nulls,
                })
                try:
                    connection.execute(
                        "INSERT INTO source_ids SELECT event_ordinal,source_ordinal,"
                        "CASE WHEN source_id__null THEN NULL ELSE source_id END "
                        "FROM turn_source_batch ORDER BY event_ordinal,source_ordinal"
                    )
                finally:
                    connection.unregister("turn_source_batch")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            if registered:
                connection.unregister("turn_event_batch")
        self._count += len(events)

    @staticmethod
    def _point_values(point: PointRef | None) -> tuple[object, ...]:
        if point is None:
            return (None,) * 7
        return (point.source_id, point.source_row_key, point.stable, point.from_ref,
                point.to_ref, point.generated_reason, point.source_row_hash)

    @staticmethod
    def _refined_values(refined: RefinedPointRef | None) -> tuple[object, ...]:
        if refined is None:
            return (None,) * 4
        return (refined.point_kind, refined.point_id, refined.subtrack_index,
                refined.refined_sequence_in_subtrack)

    def seal(self) -> None:
        if self._sealed:
            return
        connection = self._require_open()
        invalid = connection.execute("""
            SELECT count(*) FROM turn_events e WHERE
               e.event_calculate_index IS NULL OR e.event_calculate_index<>e.ordinal
               OR e.vehicle_id IS NULL OR e.vehicle_id='' OR e.source IS NULL OR e.source=''
               OR e.timestamp IS NULL OR NOT isfinite(e.timestamp)
               OR e.lon IS NULL OR NOT isfinite(e.lon) OR abs(e.lon)>180
               OR e.lat IS NULL OR NOT isfinite(e.lat) OR abs(e.lat)>90
               OR e.x IS NULL OR NOT isfinite(e.x) OR e.y IS NULL OR NOT isfinite(e.y)
               OR e.entry_bearing IS NULL OR NOT isfinite(e.entry_bearing)
               OR e.exit_bearing IS NULL OR NOT isfinite(e.exit_bearing)
               OR e.turn_deg IS NULL OR NOT isfinite(e.turn_deg)
               OR e.turn_deg < -180 OR e.turn_deg >= 180
               OR e.score IS NULL OR NOT isfinite(e.score) OR e.score<0
               OR e.score<>abs(e.turn_deg)
               OR (e.cross_track_calculate_index IS NOT NULL
                   AND e.cross_track_calculate_index<0)
               OR (e.g_star_z IS NOT NULL AND NOT isfinite(e.g_star_z))
               OR ((e.point_source_id IS NULL)::INTEGER
                  +(e.point_source_row_key IS NULL)::INTEGER
                  +(e.point_stable IS NULL)::INTEGER NOT IN (0,3))
               OR (e.point_source_id IS NULL AND (e.point_from_ref IS NOT NULL
                   OR e.point_to_ref IS NOT NULL OR e.point_generated_reason IS NOT NULL
                   OR e.point_source_row_hash IS NOT NULL))
               OR (e.point_source_id IS NOT NULL
                   AND (e.point_source_id='' OR e.point_source_row_key=''
                        OR e.point_generated_reason IS NULL OR e.point_source_row_hash IS NULL))
               OR ((e.refined_point_kind IS NULL)::INTEGER
                  +(e.refined_point_id IS NULL)::INTEGER
                  +(e.refined_subtrack_index IS NULL)::INTEGER
                  +(e.refined_sequence IS NULL)::INTEGER NOT IN (0,4))
               OR (e.refined_point_kind IS NOT NULL
                   AND (e.refined_point_kind NOT IN ('kept','injected')
                        OR e.refined_point_id<0 OR e.refined_subtrack_index<0
                        OR e.refined_sequence<0))
        """).fetchone()[0]
        ordinal_error = connection.execute("""
            SELECT count(*) FROM (
                SELECT ordinal,row_number() OVER (ORDER BY ordinal)-1 expected
                FROM turn_events
            ) WHERE ordinal<>expected
        """).fetchone()[0]
        source_error = connection.execute("""
            WITH stats AS (
                SELECT event_ordinal,count(*) n,count(source_ordinal) present,
                       min(source_ordinal) lo,max(source_ordinal) hi,
                       count(DISTINCT source_ordinal) unique_n
                FROM source_ids GROUP BY event_ordinal
            )
            SELECT count(*) FROM stats s LEFT JOIN turn_events e ON e.ordinal=s.event_ordinal
            WHERE e.ordinal IS NULL OR s.present<>s.n OR s.lo<>0 OR s.hi<>s.n-1
               OR s.unique_n<>s.n
        """).fetchone()[0]
        bad_source = connection.execute(
            "SELECT count(*) FROM source_ids WHERE source_id IS NULL OR source_id=''"
        ).fetchone()[0]
        duplicate = connection.execute("""
            SELECT count(*) FROM (
                SELECT event_calculate_index FROM turn_events
                GROUP BY event_calculate_index HAVING count(*)<>1
            )
        """).fetchone()[0]
        if invalid or ordinal_error or source_error or bad_source or duplicate:
            self.close()
            raise ValueError("invalid TurnEvent relation")
        self._sealed = True

    def __len__(self) -> int:
        self._require_open()
        return self._count

    def __iter__(self) -> Iterator[TurnEvent]:
        return self._iterate("", ())

    def __getitem__(self, index: int | slice) -> TurnEvent | tuple[TurnEvent, ...]:
        self._require_sealed()
        if isinstance(index, slice):
            return tuple(self[item] for item in range(*index.indices(len(self))))
        if not isinstance(index, int):
            raise TypeError("turn event index must be an integer or slice")
        ordinal = index + len(self) if index < 0 else index
        if ordinal < 0 or ordinal >= len(self):
            raise IndexError(index)
        iterator = self._iterate("WHERE e.ordinal=?", (ordinal,))
        try:
            return next(iterator)
        finally:
            iterator.close()

    def _iterate(self, where: str, params: tuple[object, ...]) -> Iterator[TurnEvent]:
        cursor = self._require_sealed().cursor()
        try:
            cursor.execute(
                "SELECT e.*,s.source_ordinal,s.source_id FROM turn_events e "
                "LEFT JOIN source_ids s ON s.event_ordinal=e.ordinal "
                f"{where} ORDER BY e.ordinal,s.source_ordinal", params,
            )
            current: tuple[object, ...] | None = None
            source_ids: list[str] = []
            while rows := cursor.fetchmany(self._batch_size):
                for row in rows:
                    scalar = row[:26]
                    if current is not None and scalar[0] != current[0]:
                        yield self._decode(current, source_ids)
                        source_ids = []
                    current = scalar
                    if row[26] is not None:
                        source_ids.append(row[27])
            if current is not None:
                yield self._decode(current, source_ids)
        finally:
            cursor.close()

    @staticmethod
    def _decode(row: tuple[object, ...], source_ids: list[str]) -> TurnEvent:
        point = None if row[13] is None else PointRef(
            source_id=row[13], source_row_key=row[14], stable=row[15],
            from_ref=row[16], to_ref=row[17], generated_reason=row[18],
            source_row_hash=row[19],
        )
        refined = None if row[20] is None else RefinedPointRef(
            point_kind=row[20], point_id=row[21], subtrack_index=row[22],
            refined_sequence_in_subtrack=row[23],
        )
        return TurnEvent(
            event_calculate_index=row[1], vehicle_id=row[2], timestamp=row[3],
            lon=row[4], lat=row[5], x=row[6], y=row[7], entry_bearing=row[8],
            exit_bearing=row[9], turn_deg=row[10], score=row[11], source=row[12],
            point_ref=point, refined_point_ref=refined,
            cross_track_calculate_index=row[24], g_star_z=row[25],
            source_ids=tuple(source_ids),
        )

    def iter_coordinates(self) -> Iterator[TurnCoordinate]:
        cursor = self._require_sealed().cursor()
        try:
            cursor.execute(
                "SELECT event_calculate_index,x,y FROM turn_events ORDER BY ordinal"
            )
            while rows := cursor.fetchmany(self._batch_size):
                for row in rows:
                    yield TurnCoordinate(*row)
        finally:
            cursor.close()

    def _require_sealed(self) -> duckdb.DuckDBPyConnection:
        connection = self._require_open()
        if not self._sealed:
            raise RuntimeError("turn event relation is not sealed")
        return connection

    def _require_open(self) -> duckdb.DuckDBPyConnection:
        if self._connection is None:
            raise RuntimeError("turn event relation is closed")
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if hasattr(self, "_temp_directory"):
            try:
                self._temp_directory.rmdir()
            except OSError:
                pass
        self._sealed = False

    def __enter__(self) -> TurnEventRelation:
        self._require_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
