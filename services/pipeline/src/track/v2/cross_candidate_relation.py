"""DuckDB-managed normalized relation for transient cross-track candidates."""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import duckdb

from .member_sequences import EventSummary, ReadOnlySequence
from .models import CrossTrackCandidate, PointRef, RefinedPointRef, RuntimeConfig, TurnEvent


@dataclass(frozen=True, slots=True)
class CrossCandidateCoordinate:
    cross_track_calculate_index: int
    x: float
    y: float


class CrossCandidateRelation(ReadOnlySequence[CrossTrackCandidate]):
    """Append once, validate, then repeatedly stream cross-track candidates."""

    _POINT_COLUMNS = (
        "source_id", "source_row_key", "stable", "from_ref", "to_ref",
        "generated_reason", "source_row_hash",
    )

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
        parent = root / "duckdb" / "cross-candidate-relations"
        parent.mkdir(parents=True, exist_ok=True)
        self._temp_directory = parent / identity
        self._temp_directory.mkdir(exist_ok=False)
        self._connection: duckdb.DuckDBPyConnection | None = None
        self._batch_size, self._count, self._sealed = batch_size, 0, False
        self._next_pass = 0
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
        point_fields = ",".join(
            f"{prefix}_{name} {('BOOLEAN' if name == 'stable' else 'VARCHAR')}"
            for prefix in ("point", "from_point", "to_point")
            for name in self._POINT_COLUMNS
        )
        self._require_open().execute(f"""
            CREATE TABLE cross_candidates (
                ordinal BIGINT NOT NULL,{point_fields},vehicle_id VARCHAR,
                timestamp DOUBLE,lon DOUBLE,lat DOUBLE,x DOUBLE,y DOUBLE,
                heading DOUBLE,speed DOUBLE,
                from_kind VARCHAR,from_id BIGINT,from_subtrack BIGINT,from_sequence BIGINT,
                to_kind VARCHAR,to_id BIGINT,to_subtrack BIGINT,to_sequence BIGINT,
                cross_track_calculate_index BIGINT
            )
        """)
        self._require_open().execute("""
            CREATE TABLE cross_candidate_members (
                pass_id BIGINT NOT NULL,label BIGINT NOT NULL,
                member_order BIGINT NOT NULL,ordinal BIGINT NOT NULL,
                PRIMARY KEY(pass_id,label,member_order)
            )
        """)

    def append(self, candidate: CrossTrackCandidate) -> None:
        if self._sealed:
            raise RuntimeError("cross candidate relation is sealed")
        connection, ordinal = self._require_open(), self._count
        values = (
            ordinal, *self._point_values(candidate.point_ref),
            *self._point_values(candidate.from_segment_start_point_ref),
            *self._point_values(candidate.to_segment_start_point_ref),
            candidate.vehicle_id, candidate.timestamp, candidate.lon, candidate.lat,
            candidate.x, candidate.y, candidate.heading, candidate.speed,
            *self._refined_values(candidate.from_segment_start),
            *self._refined_values(candidate.to_segment_start),
            candidate.cross_track_calculate_index,
        )
        connection.execute(
            "INSERT INTO cross_candidates VALUES (" + ",".join("?" for _ in values) + ")",
            values,
        )
        self._count += 1

    @staticmethod
    def _point_values(point: PointRef) -> tuple[object, ...]:
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
        point_invalid = " OR ".join(
            f"{prefix}_source_id IS NULL OR {prefix}_source_id='' "
            f"OR {prefix}_source_row_key IS NULL OR {prefix}_source_row_key='' "
            f"OR {prefix}_stable IS NULL OR {prefix}_generated_reason IS NULL "
            f"OR {prefix}_source_row_hash IS NULL"
            for prefix in ("point", "from_point", "to_point")
        )
        invalid = connection.execute(f"""
            SELECT count(*) FROM cross_candidates WHERE
                cross_track_calculate_index IS NULL
                OR cross_track_calculate_index<>ordinal OR vehicle_id IS NULL OR vehicle_id=''
                OR timestamp IS NULL OR NOT isfinite(timestamp)
                OR lon IS NULL OR NOT isfinite(lon) OR abs(lon)>180
                OR lat IS NULL OR NOT isfinite(lat) OR abs(lat)>90
                OR x IS NULL OR NOT isfinite(x) OR y IS NULL OR NOT isfinite(y)
                OR heading IS NULL OR NOT isfinite(heading)
                OR speed IS NULL OR NOT isfinite(speed) OR {point_invalid}
                OR ((from_kind IS NULL)::INTEGER+(from_id IS NULL)::INTEGER
                    +(from_subtrack IS NULL)::INTEGER+(from_sequence IS NULL)::INTEGER NOT IN (0,4))
                OR (from_kind IS NOT NULL AND (from_kind NOT IN ('kept','injected')
                    OR from_id<0 OR from_subtrack<0 OR from_sequence<0))
                OR ((to_kind IS NULL)::INTEGER+(to_id IS NULL)::INTEGER
                    +(to_subtrack IS NULL)::INTEGER+(to_sequence IS NULL)::INTEGER NOT IN (0,4))
                OR (to_kind IS NOT NULL AND (to_kind NOT IN ('kept','injected')
                    OR to_id<0 OR to_subtrack<0 OR to_sequence<0))
        """).fetchone()[0]
        ordinal_error = connection.execute("""
            SELECT count(*) FROM (
                SELECT ordinal,row_number() OVER (ORDER BY ordinal)-1 expected
                FROM cross_candidates
            ) WHERE ordinal<>expected
        """).fetchone()[0]
        duplicate = connection.execute("""
            SELECT count(*) FROM (
                SELECT cross_track_calculate_index FROM cross_candidates
                GROUP BY cross_track_calculate_index HAVING count(*)<>1
            )
        """).fetchone()[0]
        if invalid or ordinal_error or duplicate:
            self.close()
            raise ValueError("invalid CrossTrackCandidate relation")
        self._sealed = True

    def __len__(self) -> int:
        self._require_open()
        return self._count

    def __iter__(self) -> Iterator[CrossTrackCandidate]:
        return self._iterate("", ())

    def _get(self, index: int) -> CrossTrackCandidate:
        if not isinstance(index, int):
            raise TypeError("cross candidate index must be an integer or slice")
        iterator = self._iterate("WHERE ordinal=?", (index,))
        try:
            return next(iterator)
        finally:
            iterator.close()

    def _iterate(
        self, where: str, params: tuple[object, ...],
    ) -> Iterator[CrossTrackCandidate]:
        cursor = self._require_sealed().cursor()
        try:
            cursor.execute(
                f"SELECT * FROM cross_candidates {where} ORDER BY ordinal", params
            )
            while rows := cursor.fetchmany(self._batch_size):
                for row in rows:
                    yield self._decode(row)
        finally:
            cursor.close()

    @classmethod
    def _decode(cls, row: tuple[object, ...]) -> CrossTrackCandidate:
        def point(offset: int) -> PointRef:
            return PointRef(*row[offset:offset + 7])

        def refined(offset: int) -> RefinedPointRef | None:
            return None if row[offset] is None else RefinedPointRef(*row[offset:offset + 4])

        return CrossTrackCandidate(
            point_ref=point(1), from_segment_start_point_ref=point(8),
            to_segment_start_point_ref=point(15), vehicle_id=row[22], timestamp=row[23],
            lon=row[24], lat=row[25], x=row[26], y=row[27], heading=row[28], speed=row[29],
            from_segment_start=refined(30), to_segment_start=refined(34),
            cross_track_calculate_index=row[38],
        )

    def iter_coordinates(self) -> Iterator[CrossCandidateCoordinate]:
        cursor = self._require_sealed().cursor()
        try:
            cursor.execute(
                "SELECT cross_track_calculate_index,x,y FROM cross_candidates ORDER BY ordinal"
            )
            while rows := cursor.fetchmany(self._batch_size):
                for row in rows:
                    yield CrossCandidateCoordinate(*row)
        finally:
            cursor.close()

    def create_members(self, labels: Sequence[int]) -> MemberPass:
        """Create one independently owned clustering pass; negative labels are noise."""
        self.seal()
        if len(labels) != len(self):
            raise ValueError("one DBSCAN label is required per candidate")
        connection = self._require_sealed()
        pass_id = self._next_pass
        self._next_pass += 1
        positions: dict[int, int] = {}
        batch: list[tuple[int, int, int, int]] = []
        connection.execute("BEGIN TRANSACTION")
        iterator = iter(labels)
        try:
            for ordinal in range(len(self)):
                try:
                    raw_label = next(iterator)
                except StopIteration as exc:
                    raise ValueError(
                        "one DBSCAN label is required per candidate"
                    ) from exc
                label = int(raw_label)
                if label < 0:
                    continue
                position = positions.get(label, 0)
                positions[label] = position + 1
                batch.append((pass_id, label, position, ordinal))
                if len(batch) == self._batch_size:
                    connection.executemany(
                        "INSERT INTO cross_candidate_members VALUES (?,?,?,?)", batch
                    )
                    batch.clear()
            if batch:
                connection.executemany(
                    "INSERT INTO cross_candidate_members VALUES (?,?,?,?)", batch
                )
            try:
                next(iterator)
            except StopIteration:
                pass
            else:
                raise ValueError("one DBSCAN label is required per candidate")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()
        return MemberPass(self, pass_id, positions)

    def _require_sealed(self) -> duckdb.DuckDBPyConnection:
        connection = self._require_open()
        if not self._sealed:
            raise RuntimeError("cross candidate relation is not sealed")
        return connection

    def _require_open(self) -> duckdb.DuckDBPyConnection:
        if self._connection is None:
            raise RuntimeError("cross candidate relation is closed")
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

    def __enter__(self) -> CrossCandidateRelation:
        self._require_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class MemberPass:
    """One disposable set of cluster memberships owned by a relation."""

    def __init__(
        self, relation: CrossCandidateRelation, pass_id: int, counts: dict[int, int],
    ) -> None:
        self.relation, self.pass_id, self.counts = relation, pass_id, counts
        self._closed = False

    def _check_open(self) -> None:
        self.relation._require_open()
        if self._closed:
            raise RuntimeError("member pass is closed")

    def iter_groups(self) -> Iterator[tuple[int, ClusterMemberSequence]]:
        self._check_open()
        for label in sorted(self.counts):
            yield label, ClusterMemberSequence(self, label, self.counts[label])

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.relation._connection is not None:
            self.relation._connection.execute(
                "DELETE FROM cross_candidate_members WHERE pass_id=?", (self.pass_id,)
            )
        self.counts.clear()

    def __enter__(self) -> MemberPass:
        self._check_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class ClusterMemberSequence(ReadOnlySequence[TurnEvent]):
    """Repeatable, bounded materialization of one label's member events."""

    _SOURCE = "orthogonal_cross_track"

    def __init__(self, pass_owner: MemberPass, label: int, count: int) -> None:
        self.pass_owner, self.label, self._count = pass_owner, label, count

    def __len__(self) -> int:
        return self._count

    def _get(self, index: int) -> TurnEvent:
        if not isinstance(index, int):
            raise TypeError("member index must be an integer or slice")
        self.pass_owner._check_open()
        rows = self._rows("m.member_order,c.*", "AND m.member_order=?", (index,))
        try:
            row = next(rows)
            return self._event(self.pass_owner.relation._decode(row[1:]), row[0])
        finally:
            rows.close()

    def _rows(
        self, columns: str, extra: str = "", params: tuple[object, ...] = (),
    ) -> Iterator[tuple[object, ...]]:
        self.pass_owner._check_open()
        relation = self.pass_owner.relation
        cursor = relation._require_sealed().cursor()
        try:
            cursor.execute(f"""
                SELECT {columns} FROM cross_candidate_members m
                JOIN cross_candidates c ON c.ordinal=m.ordinal
                WHERE m.pass_id=? AND m.label=? {extra} ORDER BY m.member_order
            """, (self.pass_owner.pass_id, self.label, *params))
            while rows := cursor.fetchmany(relation._batch_size):
                yield from rows
        finally:
            cursor.close()

    @classmethod
    def _event(cls, candidate: CrossTrackCandidate, index: int) -> TurnEvent:
        return TurnEvent(
            event_calculate_index=index, vehicle_id=candidate.vehicle_id,
            timestamp=candidate.timestamp, lon=candidate.lon, lat=candidate.lat,
            x=candidate.x, y=candidate.y, entry_bearing=0.0, exit_bearing=0.0,
            turn_deg=0.0, score=0.0, source=cls._SOURCE,
            point_ref=candidate.point_ref,
            cross_track_calculate_index=candidate.cross_track_calculate_index,
            source_ids=tuple(sorted({candidate.from_segment_start_point_ref.source_id,
                                     candidate.to_segment_start_point_ref.source_id})),
        )

    def __iter__(self) -> Iterator[TurnEvent]:
        rows = self._rows("m.member_order,c.*")
        try:
            for row in rows:
                yield self._event(self.pass_owner.relation._decode(row[1:]), row[0])
        finally:
            rows.close()

    def _narrow(self, columns: str) -> Iterator[tuple[object, ...]]:
        rows = self._rows(columns)
        try:
            yield from rows
        finally:
            rows.close()

    def iter_member_identities(self) -> Iterator[tuple[str, int, int | None]]:
        for member_order, ordinal in self._narrow("m.member_order,m.ordinal"):
            yield self._SOURCE, member_order, ordinal

    def iter_ordinals(self) -> Iterator[int]:
        for (ordinal,) in self._narrow("m.ordinal"):
            yield ordinal

    def iter_coordinates(self) -> Iterator[tuple[float, float]]:
        for x, y in self._narrow("c.x,c.y"):
            yield x, y

    def iter_candidates(self) -> Iterator[CrossTrackCandidate]:
        for row in self._narrow("c.*"):
            yield self.pass_owner.relation._decode(row)

    def provenance_summary(self, fallback_source: str = "") -> EventSummary:
        del fallback_source
        self.pass_owner._check_open()
        event_ids: set[str] = set()
        ref_ids: set[str] = set()
        relation = self.pass_owner.relation
        cursor = relation._require_sealed().cursor()
        try:
            cursor.execute("""
                WITH owned AS (
                    SELECT c.from_point_source_id,c.to_point_source_id,c.point_source_id
                    FROM cross_candidate_members m JOIN cross_candidates c USING (ordinal)
                    WHERE m.pass_id=? AND m.label=?
                )
                SELECT kind,source_id FROM (
                    SELECT 'event' kind,from_point_source_id source_id FROM owned
                    UNION SELECT 'event',to_point_source_id FROM owned
                    UNION SELECT 'ref',point_source_id FROM owned
                ) WHERE source_id IS NOT NULL AND source_id<>'' ORDER BY kind,source_id
            """, (self.pass_owner.pass_id, self.label))
            while rows := cursor.fetchmany(relation._batch_size):
                for kind, source_id in rows:
                    (event_ids if kind == "event" else ref_ids).add(source_id)
        finally:
            cursor.close()
        return EventSummary(
            frozenset(event_ids), {self._SOURCE: self._count} if self._count else {},
            {self._SOURCE: frozenset(ref_ids)} if self._count else {},
        )
