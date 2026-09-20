"""DuckDB relations for the first, source-association phase of Detect writing."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import duckdb

from .member_sequences import iter_member_identities
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
        self._detect_id = detect_id
        self._resolved = False
        self._annotations_prepared = False
        self._cross_validated = False
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
            );
            CREATE TABLE members (
                kind VARCHAR NOT NULL, calculation_index BIGINT NOT NULL,
                encounter BIGINT NOT NULL, candidate_id BIGINT NOT NULL
            );
            CREATE TABLE hotspots (
                calculation_index BIGINT NOT NULL, input_ordinal BIGINT NOT NULL,
                z DOUBLE
            );
            CREATE TABLE annotations (
                request_order BIGINT PRIMARY KEY, kind VARCHAR NOT NULL,
                ordinal BIGINT NOT NULL, candidate_id BIGINT, z DOUBLE
            );
            CREATE TABLE validated_cross (
                ordinal BIGINT PRIMARY KEY, detect_id VARCHAR NOT NULL,
                candidate_id BIGINT, low_resolved_id BIGINT NOT NULL,
                high_resolved_id BIGINT NOT NULL, x DOUBLE, y DOUBLE,
                lon DOUBLE, lat DOUBLE
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

    def prepare_annotations(self, clusters, candidate_ids, hotspots) -> None:
        """Associate annotation requests with the first candidate and last hotspot."""
        connection = self._require_open()
        if not self._resolved:
            raise RuntimeError("writer relation is not resolved")
        if self._annotations_prepared:
            raise RuntimeError("writer annotations are already prepared")
        connection.execute("BEGIN TRANSACTION")
        try:
            encounter = 0
            with _closing_optional(iter(clusters)) as cluster_iterator:
                for cluster in cluster_iterator:
                    candidate_id = candidate_ids[cluster.cluster_calculate_index]
                    identities = iter_member_identities(cluster.events)
                    with _closing_optional(identities):
                        for source, event_index, cross_index in identities:
                            if source == "turn":
                                kind, calculation_index = "turn", event_index
                            elif source == "orthogonal_cross_track" and cross_index is not None:
                                kind, calculation_index = "cross", cross_index
                            else:
                                continue
                            connection.execute("INSERT INTO members VALUES (?,?,?,?)", (
                                kind, calculation_index, encounter, candidate_id,
                            ))
                            encounter += 1
            with _closing_optional(iter(hotspots)) as hotspot_iterator:
                for ordinal, event in enumerate(hotspot_iterator):
                    connection.execute("INSERT INTO hotspots VALUES (?,?,?)", (
                        event.event_calculate_index, ordinal, event.g_star_z,
                    ))
            conflict = connection.execute("""
                SELECT calculation_index FROM (
                    SELECT calculation_index,encounter,candidate_id,
                           first_value(candidate_id) OVER (
                               PARTITION BY kind,calculation_index ORDER BY encounter
                           ) AS first_candidate
                    FROM members
                ) WHERE candidate_id != first_candidate
                ORDER BY encounter LIMIT 1
            """).fetchone()
            if conflict is not None:
                raise ValueError(
                    f"Detect entity {conflict[0]} belongs to multiple candidates"
                )
            connection.execute("""
                INSERT INTO annotations
                WITH unique_members AS (
                    SELECT kind,calculation_index,candidate_id FROM members
                    QUALIFY row_number() OVER (
                        PARTITION BY kind,calculation_index ORDER BY encounter
                    )=1
                ), last_hotspots AS (
                    SELECT calculation_index,z FROM hotspots
                    QUALIFY row_number() OVER (
                        PARTITION BY calculation_index ORDER BY input_ordinal DESC
                    )=1
                )
                SELECT r.request_order,r.kind,r.ordinal,m.candidate_id,
                       CASE WHEN r.kind='turn' THEN h.z ELSE NULL END
                FROM annotation_requests r
                LEFT JOIN unique_members m USING(kind,calculation_index)
                LEFT JOIN last_hotspots h
                  ON r.kind='turn' AND r.calculation_index=h.calculation_index
                ORDER BY r.request_order
            """)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        self._annotations_prepared = True

    def iter_annotations(self, kind: str) -> Iterator[tuple[int, int | None, float | None]]:
        if kind not in self._CONSUMERS[:2]:
            raise ValueError("unknown writer annotation kind")
        connection = self._require_open()
        if not self._annotations_prepared:
            raise RuntimeError("writer annotations are not prepared")
        yield from self._iterate(connection, """
            SELECT ordinal,candidate_id,z FROM annotations
            WHERE kind=? ORDER BY request_order
        """, (kind,))

    def iter_rows(self, consumer: int, detect_id: str) -> Iterator[tuple]:
        """Yield final PostgreSQL COPY rows after checking every association."""
        if consumer not in range(len(self._CONSUMERS)):
            raise ValueError("unknown source consumer")
        connection = self._require_open()
        self._require_detect_id(detect_id)
        if not self._resolved:
            raise RuntimeError("writer relation is not resolved")
        if consumer < 2 and not self._annotations_prepared:
            raise RuntimeError("writer annotations are not prepared")
        if consumer == 1 and not self._cross_validated:
            raise RuntimeError("track cross pairs are not validated")
        self._validate_final_inputs(connection, consumer)
        if consumer == 0:
            query = """
                SELECT ?,a.candidate_id,s.resolved_id,r.entry_bearing,r.exit_bearing,
                       r.turn_deg,r.score,a.z
                FROM turn_raw r
                JOIN resolved_sources s
                  ON s.consumer=0 AND s.ordinal=r.ordinal AND s.endpoint=0
                JOIN annotations a ON a.kind='turn' AND a.ordinal=r.ordinal
                ORDER BY r.ordinal
            """
        elif consumer == 1:
            query = """
                SELECT detect_id,candidate_id,low_resolved_id,high_resolved_id,
                       x,y,lon,lat FROM validated_cross ORDER BY ordinal
            """
        else:
            query = """
                SELECT r.crossing_id,?,r.candidate_id,s.resolved_id,r.is_entry,
                       r.x,r.y,r.lon,r.lat
                FROM crossing_raw r
                JOIN resolved_sources s
                  ON s.consumer=2 AND s.ordinal=r.ordinal AND s.endpoint=0
                ORDER BY r.ordinal
            """
        yield from self._iterate(connection, query, () if consumer == 1 else (detect_id,))

    def _validate_final_inputs(self, connection, consumer: int) -> None:
        kind = self._CONSUMERS[consumer]
        expected_endpoints = 2 if consumer == 1 else 1
        raw_count = connection.execute(
            f"SELECT count(*) FROM {kind}_raw"
        ).fetchone()[0]
        if raw_count != self.counts[consumer]:
            raise RuntimeError("raw writer row count mismatch")
        source_error = connection.execute(f"""
            SELECT 1 FROM (
                SELECT ordinal,endpoint FROM resolved_sources WHERE consumer=?
                EXCEPT
                SELECT r.ordinal,e.endpoint FROM {kind}_raw r
                CROSS JOIN range(?) e(endpoint)
            ) UNION ALL SELECT 1 FROM (
                SELECT r.ordinal,e.endpoint FROM {kind}_raw r
                CROSS JOIN range(?) e(endpoint)
                EXCEPT
                SELECT ordinal,endpoint FROM resolved_sources WHERE consumer=?
            ) UNION ALL SELECT 1 FROM (
                SELECT ordinal,endpoint FROM resolved_sources WHERE consumer=?
                GROUP BY ordinal,endpoint HAVING count(*) != 1
            ) LIMIT 1
        """, (consumer, expected_endpoints, expected_endpoints, consumer, consumer)).fetchone()
        if source_error is not None:
            raise RuntimeError("source association mismatch")
        if consumer < 2:
            annotation_error = connection.execute(f"""
                SELECT 1 FROM (
                    SELECT ordinal FROM annotations WHERE kind=?
                    EXCEPT SELECT ordinal FROM {kind}_raw
                ) UNION ALL SELECT 1 FROM (
                    SELECT ordinal FROM {kind}_raw
                    EXCEPT SELECT ordinal FROM annotations WHERE kind=?
                ) UNION ALL SELECT 1 FROM (
                    SELECT ordinal FROM annotations WHERE kind=?
                    GROUP BY ordinal HAVING count(*) != 1
                ) LIMIT 1
            """, (kind, kind, kind)).fetchone()
            if annotation_error is not None:
                raise RuntimeError("annotation association mismatch")

    def validate_cross_pairs(self, detect_id: str) -> None:
        connection = self._require_open()
        self._require_detect_id(detect_id)
        if self._cross_validated:
            return
        if not self._resolved:
            raise RuntimeError("writer relation is not resolved")
        if not self._annotations_prepared:
            raise RuntimeError("writer annotations are not prepared")
        connection.execute("DELETE FROM validated_cross")
        self._validate_final_inputs(connection, 1)
        invalid = connection.execute("""
            WITH pairs AS (
                SELECT least(s0.resolved_id,s1.resolved_id) low_id,
                       greatest(s0.resolved_id,s1.resolved_id) high_id
                FROM cross_raw r
                JOIN resolved_sources s0
                  ON s0.consumer=1 AND s0.ordinal=r.ordinal AND s0.endpoint=0
                JOIN resolved_sources s1
                  ON s1.consumer=1 AND s1.ordinal=r.ordinal AND s1.endpoint=1
            )
            SELECT 1 FROM pairs GROUP BY low_id,high_id
            HAVING low_id=high_id OR count(*)>1 LIMIT 1
        """).fetchone()
        if invalid is not None:
            raise ValueError("duplicate/degenerate track cross segment pair")
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute("""
                INSERT INTO validated_cross
                SELECT r.ordinal,?,a.candidate_id,
                       least(s0.resolved_id,s1.resolved_id),
                       greatest(s0.resolved_id,s1.resolved_id),r.x,r.y,r.lon,r.lat
                FROM cross_raw r
                JOIN resolved_sources s0
                  ON s0.consumer=1 AND s0.ordinal=r.ordinal AND s0.endpoint=0
                JOIN resolved_sources s1
                  ON s1.consumer=1 AND s1.ordinal=r.ordinal AND s1.endpoint=1
                JOIN annotations a ON a.kind='cross' AND a.ordinal=r.ordinal
                ORDER BY r.ordinal
            """, (detect_id,))
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            connection.execute("DELETE FROM validated_cross")
            raise
        self._cross_validated = True

    def iter_validated_cross_rows(self) -> Iterator[tuple]:
        connection = self._require_open()
        if not self._cross_validated:
            raise RuntimeError("track cross pairs are not validated")
        yield from self._iterate(connection, """
            SELECT detect_id,candidate_id,low_resolved_id,high_resolved_id,x,y,lon,lat
            FROM validated_cross ORDER BY ordinal
        """, ())

    def _require_detect_id(self, detect_id: str) -> None:
        if detect_id != self._detect_id:
            raise ValueError("detect_id does not match writer relation")

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
