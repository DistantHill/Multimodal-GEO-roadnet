"""DuckDB relations for the first, source-association phase of Detect writing."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import duckdb

from .models import RuntimeConfig


@contextmanager
def _closing_optional(iterator):
    try:
        yield iterator
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()


class WriterRelation:
    """Own normalized raw writer rows and resolve exact Refine source keys."""

    _CONSUMERS = ("turn", "cross", "crossing")

    def __init__(
        self, runtime: RuntimeConfig, data_root: str | Path, detect_id: str, *,
        batch_size: int = 1024, relation_id: str | None = None,
    ) -> None:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("batch_size must be positive")
        root = Path(data_root)
        if not root.is_absolute() or root == Path(root.anchor):
            raise ValueError("data_root must be absolute and non-root")
        self._validate_identity(detect_id, "detect_id")
        identity = relation_id or uuid4().hex
        self._validate_identity(identity, "relation_id")
        parent = root / "duckdb" / "writer-relations" / detect_id
        parent.mkdir(parents=True, exist_ok=True)
        self._temp_directory = parent / identity
        self._temp_directory.mkdir(exist_ok=False)
        self._connection: duckdb.DuckDBPyConnection | None = None
        self._batch_size = batch_size
        self._resolved = False
        self._request_order = 0
        self._mapping_order = 0
        self.counts = [0, 0, 0]
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
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _validate_identity(value: str, name: str) -> None:
        if (not isinstance(value, str) or not value or value in {".", ".."}
                or "\x00" in value or Path(value).name != value):
            raise ValueError(f"{name} must be one path component")

    def _create_schema(self) -> None:
        self._require_open().execute("""
            CREATE TABLE source_requests (
                request_order BIGINT PRIMARY KEY, point_kind VARCHAR NOT NULL,
                point_id BIGINT NOT NULL, subtrack_index BIGINT NOT NULL,
                consumer INTEGER NOT NULL, ordinal BIGINT NOT NULL, endpoint INTEGER NOT NULL
            );
            CREATE TABLE source_mappings (
                mapping_order BIGINT PRIMARY KEY, point_kind VARCHAR NOT NULL,
                point_id BIGINT NOT NULL, subtrack_index BIGINT NOT NULL,
                resolved_id BIGINT NOT NULL
            );
            CREATE TABLE turn_raw (
                ordinal BIGINT PRIMARY KEY, entry_bearing DOUBLE, exit_bearing DOUBLE,
                turn_deg DOUBLE, score DOUBLE
            );
            CREATE TABLE cross_raw (
                ordinal BIGINT PRIMARY KEY, x DOUBLE, y DOUBLE, lon DOUBLE, lat DOUBLE
            );
            CREATE TABLE crossing_raw (
                ordinal BIGINT PRIMARY KEY, crossing_id BIGINT, candidate_id BIGINT,
                is_entry INTEGER, x DOUBLE, y DOUBLE, lon DOUBLE, lat DOUBLE
            );
            CREATE TABLE annotation_requests (
                request_order BIGINT PRIMARY KEY, kind VARCHAR NOT NULL,
                calculation_index BIGINT NOT NULL, ordinal BIGINT NOT NULL
            );
            CREATE TABLE resolved_sources (
                consumer INTEGER NOT NULL, ordinal BIGINT NOT NULL,
                endpoint INTEGER NOT NULL, resolved_id BIGINT NOT NULL
            )
        """)

    def _require_appendable(self) -> duckdb.DuckDBPyConnection:
        connection = self._require_open()
        if self._resolved:
            raise RuntimeError("writer relation is resolved")
        return connection

    def add_request(self, consumer: int, ordinal: int, endpoint: int, ref: object) -> None:
        connection = self._require_appendable()
        connection.execute(
            "INSERT INTO source_requests VALUES (?,?,?,?,?,?,?)",
            (self._request_order, ref.point_kind, ref.point_id, ref.subtrack_index,
             consumer, ordinal, endpoint),
        )
        self._request_order += 1

    def collect_sources(self, result, pending, crossing_ids, candidate_ids) -> None:
        connection = self._require_appendable()
        annotation_order = connection.execute(
            "SELECT count(*) FROM annotation_requests"
        ).fetchone()[0]
        with _closing_optional(iter(result.turn_events)) as turns:
            for ordinal, event in enumerate(turns, self.counts[0]):
                if event.refined_point_ref is None:
                    raise ValueError("a persisted turn event requires a stable resolved point")
                self.add_request(0, ordinal, 0, event.refined_point_ref)
                connection.execute("INSERT INTO turn_raw VALUES (?,?,?,?,?)", (
                    ordinal, event.entry_bearing, event.exit_bearing, event.turn_deg, event.score,
                ))
                connection.execute("INSERT INTO annotation_requests VALUES (?,?,?,?)", (
                    annotation_order, "turn", event.event_calculate_index, ordinal,
                ))
                annotation_order += 1
                self.counts[0] += 1
        with _closing_optional(iter(result.cross_track_candidates)) as crosses:
            for ordinal, candidate in enumerate(crosses, self.counts[1]):
                for endpoint, ref in enumerate((candidate.from_segment_start,
                                                candidate.to_segment_start)):
                    if ref is None:
                        raise ValueError("a persisted track cross requires stable segment starts")
                    self.add_request(1, ordinal, endpoint, ref)
                connection.execute("INSERT INTO cross_raw VALUES (?,?,?,?,?)", (
                    ordinal, candidate.x, candidate.y, candidate.lon, candidate.lat,
                ))
                connection.execute("INSERT INTO annotation_requests VALUES (?,?,?,?)", (
                    annotation_order, "cross", candidate.cross_track_calculate_index, ordinal,
                ))
                annotation_order += 1
                self.counts[1] += 1
        with _closing_optional(iter(pending)) as pending_iterator, \
             _closing_optional(iter(crossing_ids)) as id_iterator:
            for ordinal, ((cluster, crossing), crossing_id) in enumerate(
                zip(pending_iterator, id_iterator, strict=True), self.counts[2]
            ):
                if crossing.segment_start is None:
                    raise ValueError("a persisted crossing requires a stable segment start")
                self.add_request(2, ordinal, 0, crossing.segment_start)
                connection.execute("INSERT INTO crossing_raw VALUES (?,?,?,?,?,?,?,?)", (
                    ordinal, crossing_id, candidate_ids[cluster.cluster_calculate_index],
                    int(crossing.is_entry), crossing.crossing_x, crossing.crossing_y,
                    crossing.crossing_lon, crossing.crossing_lat,
                ))
                self.counts[2] += 1

    def add_mapping(self, ref: object, resolved_id: int) -> None:
        self._require_appendable().execute(
            "INSERT INTO source_mappings VALUES (?,?,?,?,?)",
            (self._mapping_order, ref.point_kind, ref.point_id, ref.subtrack_index,
             resolved_id),
        )
        self._mapping_order += 1

    def resolve_sources(self) -> None:
        connection = self._require_open()
        if self._resolved:
            return
        missing = connection.execute("""
            SELECT r.point_kind,r.point_id,r.subtrack_index
            FROM source_requests r LEFT JOIN source_mappings m
              USING(point_kind,point_id,subtrack_index)
            WHERE m.mapping_order IS NULL ORDER BY r.request_order LIMIT 1
        """).fetchone()
        if missing is not None:
            raise KeyError(tuple(missing))
        connection.execute("""
            INSERT INTO resolved_sources
            SELECT r.consumer,r.ordinal,r.endpoint,m.resolved_id
            FROM source_requests r JOIN source_mappings m
              USING(point_kind,point_id,subtrack_index)
            QUALIFY row_number() OVER (
                PARTITION BY r.request_order ORDER BY m.mapping_order DESC
            )=1
        """)
        self._resolved = True

    def iter_resolved_sources(self, consumer: int) -> Iterator[tuple[int, int, int]]:
        if consumer not in range(len(self._CONSUMERS)):
            raise ValueError("unknown source consumer")
        connection = self._require_open()
        if not self._resolved:
            raise RuntimeError("writer relation is not resolved")
        yield from self._iterate(connection, """
            SELECT ordinal,endpoint,resolved_id FROM resolved_sources
            WHERE consumer=? ORDER BY ordinal,endpoint
        """, (consumer,))

    def iter_raw(self, kind: str) -> Iterator[tuple]:
        if kind not in self._CONSUMERS:
            raise ValueError("unknown raw writer relation")
        yield from self._iterate(
            self._require_open(), f"SELECT * FROM {kind}_raw ORDER BY ordinal", (),
        )

    def iter_annotation_requests(self) -> Iterator[tuple[str, int, int]]:
        yield from self._iterate(self._require_open(), """
            SELECT kind,calculation_index,ordinal FROM annotation_requests
            ORDER BY request_order
        """, ())

    def _iterate(self, connection, query: str, params: tuple) -> Iterator[tuple]:
        cursor = connection.cursor()
        try:
            cursor.execute(query, params)
            while rows := cursor.fetchmany(self._batch_size):
                yield from rows
        finally:
            cursor.close()

    def _require_open(self) -> duckdb.DuckDBPyConnection:
        if self._connection is None:
            raise RuntimeError("writer relation is closed")
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

    def __enter__(self) -> WriterRelation:
        self._require_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
