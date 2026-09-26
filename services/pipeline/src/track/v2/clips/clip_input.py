"""DuckDB-managed temporary columnar relation for Clip/Fitting handoff."""
from __future__ import annotations

from collections.abc import Iterator, Sequence
import numpy as np
from pathlib import Path
from uuid import uuid4

import duckdb

from ..models import RuntimeConfig, TrackClip


class ClipRelation(Sequence[TrackClip]):
    """Build once, validate, then repeatedly read ``TrackClip`` rows."""

    def __init__(
        self, runtime: RuntimeConfig, data_root: str | Path, *, batch_size: int | None = None,
        relation_id: str | None = None,
    ) -> None:
        batch_size = runtime.relation_batch_size if batch_size is None else batch_size
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("batch_size must be positive")
        root = Path(data_root)
        if not root.is_absolute():
            raise ValueError("data_root must be absolute")
        identity = relation_id or uuid4().hex
        if (
            not identity or identity in {".", ".."} or "\x00" in identity
            or Path(identity).name != identity
        ):
            raise ValueError("relation_id must be one path component")
        parent = root / "duckdb" / "clip-relations"
        parent.mkdir(parents=True, exist_ok=True)
        temp = parent / identity
        temp.mkdir(exist_ok=False)
        self._temp_directory = temp
        self._connection: duckdb.DuckDBPyConnection | None = None
        self._batch_size = batch_size
        self._sealed = False
        self._count = 0
        try:
            self._connection = duckdb.connect(
                database=":memory:",
                config={
                    "allow_unsigned_extensions": "false",
                    "autoload_known_extensions": "false",
                    "autoinstall_known_extensions": "false",
                    "memory_limit": runtime.duckdb_memory_limit,
                    "threads": str(runtime.duckdb_threads),
                    "temp_directory": str(temp),
                    "max_temp_directory_size": runtime.duckdb_max_temp_directory_size,
                },
            )
            self._create_schema()
        except Exception:
            self.close()
            raise

    def _create_schema(self) -> None:
        connection = self._require_open()
        connection.execute("""
            CREATE TABLE clips (
                ordinal BIGINT NOT NULL, clip_id VARCHAR, vehicle_id VARCHAR,
                trip_id VARCHAR, clip_type VARCHAR,
                start_kind VARCHAR, start_lon DOUBLE, start_lat DOUBLE,
                start_intersection_id BIGINT, start_crossing_id BIGINT,
                end_kind VARCHAR, end_lon DOUBLE, end_lat DOUBLE,
                end_intersection_id BIGINT, end_crossing_id BIGINT,
                distance_m DOUBLE, point_count BIGINT, subtrack_index BIGINT,
                clip_ordinal_in_track BIGINT, start_index BIGINT, end_index BIGINT,
                interior_start BIGINT, interior_end BIGINT,
                start_x DOUBLE, start_y DOUBLE, end_x DOUBLE, end_y DOUBLE
            );
            CREATE TABLE clip_points (
                clip_ordinal BIGINT NOT NULL, point_ordinal BIGINT,
                resolved_point_id BIGINT, lon DOUBLE, lat DOUBLE, x DOUBLE, y DOUBLE
            )
        """)

    def append(self, clip: TrackClip) -> None:
        if self._sealed:
            raise RuntimeError("clip relation is sealed")
        connection = self._require_open()
        ordinal = self._count
        connection.execute("BEGIN")
        try:
            connection.execute(
                "INSERT INTO clips VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ordinal, clip.clip_id, clip.vehicle_id, clip.trip_id, clip.clip_type,
                    clip.start_kind, float(clip.lonlat[0, 0]), float(clip.lonlat[0, 1]),
                    clip.start_intersection_id, clip.start_crossing_id,
                    clip.end_kind, float(clip.lonlat[-1, 0]), float(clip.lonlat[-1, 1]),
                    clip.end_intersection_id, clip.end_crossing_id,
                    clip.distance_m, clip.point_count, clip.subtrack_index,
                    clip.clip_ordinal_in_track, clip.start_index, clip.end_index,
                    clip.interior_start, clip.interior_end,
                    float(clip.xy[0,0]), float(clip.xy[0,1]),
                    float(clip.xy[-1,0]), float(clip.xy[-1,1]),
                ),
            )
            for lo in range(0, len(clip.resolved_point_ids), self._batch_size):
                hi = min(lo+self._batch_size, len(clip.resolved_point_ids))
                connection.execute(
                    "INSERT INTO clip_points SELECT ?, unnest(?::BIGINT[]), "
                    "unnest(?::BIGINT[]), unnest(?::DOUBLE[]), unnest(?::DOUBLE[]), "
                    "unnest(?::DOUBLE[]), unnest(?::DOUBLE[])",
                    (ordinal, np.arange(lo, hi, dtype=np.int64).tolist(),
                     clip.resolved_point_ids[lo:hi].tolist(),
                     clip.lonlat[lo+1:hi+1,0].tolist(), clip.lonlat[lo+1:hi+1,1].tolist(),
                     clip.xy[lo+1:hi+1,0].tolist(), clip.xy[lo+1:hi+1,1].tolist()),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        self._count += 1

    def seal(self) -> None:
        if self._sealed:
            return
        connection = self._require_open()
        invalid = connection.execute("""
            WITH point_stats AS (
                SELECT clip_ordinal, count(*) AS n, count(point_ordinal) AS ordinals,
                       min(point_ordinal) AS lo, max(point_ordinal) AS hi,
                       count(DISTINCT point_ordinal) AS unique_ordinals
                FROM clip_points GROUP BY clip_ordinal
            )
            SELECT count(*) FROM clips c LEFT JOIN point_stats p ON p.clip_ordinal=c.ordinal
            WHERE c.clip_id IS NULL OR c.clip_id='' OR c.vehicle_id IS NULL OR c.vehicle_id=''
               OR c.trip_id IS NULL OR c.trip_id='' OR c.clip_type IS NULL OR c.clip_type=''
               OR c.start_kind IS NULL OR c.start_kind='' OR c.end_kind IS NULL OR c.end_kind=''
               OR c.clip_type NOT IN ('between_intersection','boundary_start','boundary_end','full_track')
               OR c.start_kind NOT IN ('entry','exit','boundary')
               OR c.end_kind NOT IN ('entry','exit','boundary')
               OR (c.start_kind='boundary' AND
                   (c.start_intersection_id IS NOT NULL OR c.start_crossing_id IS NOT NULL))
               OR (c.end_kind='boundary' AND
                   (c.end_intersection_id IS NOT NULL OR c.end_crossing_id IS NOT NULL))
               OR (c.start_kind IN ('entry','exit') AND
                   (c.start_intersection_id IS NULL OR c.start_crossing_id IS NULL))
               OR (c.end_kind IN ('entry','exit') AND
                   (c.end_intersection_id IS NULL OR c.end_crossing_id IS NULL))
               OR (c.clip_type='between_intersection' AND
                   (c.start_kind<>'exit' OR c.end_kind<>'entry'))
               OR (c.clip_type='boundary_end' AND
                   (c.start_kind<>'boundary' OR c.end_kind<>'entry'))
               OR (c.clip_type='boundary_start' AND
                   (c.start_kind<>'exit' OR c.end_kind<>'boundary'))
               OR (c.clip_type='full_track' AND
                   (c.start_kind<>'boundary' OR c.end_kind<>'boundary'))
               OR c.start_lon IS NULL OR NOT isfinite(c.start_lon) OR abs(c.start_lon)>180
               OR c.start_lat IS NULL OR NOT isfinite(c.start_lat) OR abs(c.start_lat)>90
               OR c.end_lon IS NULL OR NOT isfinite(c.end_lon) OR abs(c.end_lon)>180
               OR c.end_lat IS NULL OR NOT isfinite(c.end_lat) OR abs(c.end_lat)>90
               OR c.distance_m IS NULL OR NOT isfinite(c.distance_m) OR c.distance_m<=0
               OR c.point_count IS NULL OR c.point_count <> coalesce(p.n, 0) + 2
               OR c.subtrack_index IS NULL OR c.subtrack_index<0
               OR c.clip_ordinal_in_track IS NULL OR c.clip_ordinal_in_track<0
               OR c.start_index IS NULL OR c.start_index<0
               OR c.end_index IS NULL OR c.end_index<c.start_index
               OR c.interior_start IS NULL OR c.interior_start<c.start_index+1
               OR c.interior_end IS NULL OR c.interior_end<c.interior_start
               OR c.interior_end>c.end_index
               OR c.interior_end-c.interior_start<>coalesce(p.n,0)
               OR c.start_x IS NULL OR NOT isfinite(c.start_x)
               OR c.start_y IS NULL OR NOT isfinite(c.start_y)
               OR c.end_x IS NULL OR NOT isfinite(c.end_x)
               OR c.end_y IS NULL OR NOT isfinite(c.end_y)
               OR coalesce(p.ordinals, 0) <> coalesce(p.n, 0)
               OR (coalesce(p.n, 0) > 0 AND
                   (p.lo <> 0 OR p.hi <> p.n - 1 OR p.unique_ordinals <> p.n))
        """).fetchone()[0]
        duplicate_ids = connection.execute(
            "SELECT count(*) FROM (SELECT clip_id FROM clips GROUP BY clip_id HAVING count(*)<>1)"
        ).fetchone()[0]
        duplicate_track_ordinals = connection.execute("""
            SELECT count(*) FROM (
                SELECT vehicle_id,subtrack_index,clip_ordinal_in_track FROM clips
                GROUP BY vehicle_id,subtrack_index,clip_ordinal_in_track HAVING count(*)<>1
            )
        """).fetchone()[0]
        orphan_or_invalid_points = connection.execute("""
            SELECT count(*) FROM clip_points p LEFT JOIN clips c ON c.ordinal=p.clip_ordinal
            WHERE c.ordinal IS NULL OR p.point_ordinal IS NULL OR p.resolved_point_id IS NULL
               OR p.lon IS NULL OR NOT isfinite(p.lon) OR abs(p.lon)>180
               OR p.lat IS NULL OR NOT isfinite(p.lat) OR abs(p.lat)>90
               OR p.x IS NULL OR NOT isfinite(p.x) OR p.y IS NULL OR NOT isfinite(p.y)
        """).fetchone()[0]
        ordinal_error = connection.execute("""
            SELECT count(*) FROM (
                SELECT ordinal, row_number() OVER (ORDER BY ordinal)-1 AS expected FROM clips
            ) WHERE ordinal<>expected
        """).fetchone()[0]
        if (
            invalid or duplicate_ids or duplicate_track_ordinals
            or orphan_or_invalid_points or ordinal_error
        ):
            self.close()
            raise ValueError("invalid Clip relation")
        self._sealed = True

    def __len__(self) -> int:
        self._require_open()
        return self._count

    def __iter__(self) -> Iterator[TrackClip]:
        return self._iterate("", (), "c.ordinal")

    def __getitem__(self, index: int | slice) -> TrackClip | tuple[TrackClip, ...]:
        self._require_sealed()
        if isinstance(index, slice):
            return tuple(self[item] for item in range(*index.indices(len(self))))
        if not isinstance(index, int):
            raise TypeError("clip index must be an integer or slice")
        ordinal = index + len(self) if index < 0 else index
        if ordinal < 0 or ordinal >= len(self):
            raise IndexError(index)
        iterator = self._iterate("WHERE c.ordinal=?", (ordinal,), "c.ordinal")
        try:
            return next(iterator)
        finally:
            iterator.close()

    def get_by_clip_id(self, clip_id: str) -> TrackClip:
        self._require_sealed()
        iterator = self._iterate("WHERE c.clip_id=?", (clip_id,), "c.ordinal")
        try:
            return next(iterator)
        except StopIteration as exc:
            raise KeyError(clip_id) from exc
        finally:
            iterator.close()

    def iter_pair(
        self, pair: tuple[int, int], *, reverse: bool | None = None,
    ) -> Iterator[TrackClip]:
        where, params = self._pair_filter(pair, reverse)
        if not where:
            return iter(())
        return self._iterate(where, params, "c.clip_id,c.ordinal")

    def count_pair(self, pair: tuple[int, int], *, reverse: bool | None = None) -> int:
        connection = self._require_sealed()
        where, params = self._pair_filter(pair, reverse)
        if not where:
            return 0
        return connection.execute(f"SELECT count(*) FROM clips c {where}", params).fetchone()[0]

    @staticmethod
    def _pair_filter(
        pair: tuple[int, int], reverse: bool | None,
    ) -> tuple[str, tuple[object, ...]]:
        low, high = sorted(pair)
        if low == high:
            return "", ()
        direction = ""
        if reverse is False:
            direction = " AND start_intersection_id=? AND end_intersection_id=?"
            params: tuple[object, ...] = (low, high, low, high)
        elif reverse is True:
            direction = " AND start_intersection_id=? AND end_intersection_id=?"
            params = (low, high, high, low)
        else:
            params = (low, high)
        return (
            "WHERE least(c.start_intersection_id,c.end_intersection_id)=? "
            "AND greatest(c.start_intersection_id,c.end_intersection_id)=?" + direction,
            params,
        )

    def _iterate(
        self, where: str, params: tuple[object, ...], order_by: str,
    ) -> Iterator[TrackClip]:
        connection = self._require_sealed()
        cursor = connection.cursor()
        try:
            cursor.execute(f"SELECT c.* FROM clips c {where} ORDER BY {order_by}", params)
            while rows := cursor.fetchmany(self._batch_size):
                for row in rows:
                    values = connection.execute(
                        "SELECT resolved_point_id,lon,lat,x,y FROM clip_points "
                        "WHERE clip_ordinal=? ORDER BY point_ordinal", (row[0],),
                    ).fetchnumpy()
                    yield self._decode(row, values)
        finally:
            cursor.close()

    @staticmethod
    def _decode(row: tuple[object, ...], values: dict[str, np.ndarray]) -> TrackClip:
        count = len(values["resolved_point_id"])
        lonlat = np.empty((count+2, 2), dtype=np.float64)
        xy = np.empty_like(lonlat)
        lonlat[0], lonlat[-1] = (row[6],row[7]), (row[11],row[12])
        xy[0], xy[-1] = (row[23],row[24]), (row[25],row[26])
        lonlat[1:-1,0], lonlat[1:-1,1] = values["lon"], values["lat"]
        xy[1:-1,0], xy[1:-1,1] = values["x"], values["y"]
        return TrackClip(
            clip_id=row[1], vehicle_id=row[2], trip_id=row[3], clip_type=row[4],
            start_kind=row[5], start_intersection_id=row[8], start_crossing_id=row[9],
            end_kind=row[10], end_intersection_id=row[13], end_crossing_id=row[14],
            xy=xy, lonlat=lonlat, resolved_point_ids=values["resolved_point_id"],
            distance_m=row[15], point_count=row[16], subtrack_index=row[17],
            clip_ordinal_in_track=row[18], start_index=row[19], end_index=row[20],
            interior_start=row[21], interior_end=row[22],
        )

    def _require_sealed(self) -> duckdb.DuckDBPyConnection:
        connection = self._require_open()
        if not self._sealed:
            raise RuntimeError("clip relation is not sealed")
        return connection

    def _require_open(self) -> duckdb.DuckDBPyConnection:
        if self._connection is None:
            raise RuntimeError("clip relation is closed")
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

    def __enter__(self) -> ClipRelation:
        self._require_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
