"""DuckDB relations for the first, source-association phase of Detect writing."""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from itertools import islice
import math
from numbers import Integral
from pathlib import Path
from uuid import uuid4

import duckdb
import numpy as np

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


def _integer_array(values, *, nullable: bool = False):
    data, nulls = [], []
    for value in values:
        is_null = value is None
        if is_null and nullable:
            data.append(0)
        elif (isinstance(value, bool) or not isinstance(value, Integral)
              or not -(2**63) <= int(value) < 2**63):
            raise TypeError("writer integer field has an invalid type or range")
        else:
            data.append(int(value))
        nulls.append(is_null)
    return np.asarray(data, dtype=np.int64), np.asarray(nulls, dtype=np.bool_)


def _float_arrays(values):
    data, nulls, nans = [], [], []
    for value in values:
        is_null = value is None
        number = 0.0 if is_null else float(value)
        data.append(0.0 if math.isnan(number) else number)
        nulls.append(is_null)
        nans.append(not is_null and math.isnan(number))
    return (np.asarray(data, dtype=np.float64), np.asarray(nulls, dtype=np.bool_),
            np.asarray(nans, dtype=np.bool_))


def _string_array(values):
    values = list(values)
    if any(not isinstance(value, str) for value in values):
        raise TypeError("writer string field has an invalid type")
    return np.asarray(values, dtype=np.str_)


def _next_validated_batch(iterator, size: int, validate):
    batch = []
    for _ in range(size):
        try:
            item = next(iterator)
        except StopIteration:
            break
        validate(item)
        batch.append(item)
    return batch


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

    def _register_batch(self, columns: dict[str, np.ndarray]) -> str:
        relation = f"writer_batch_{uuid4().hex}"
        self._require_appendable().register(relation, columns)
        return relation

    @staticmethod
    def _float_batch(rows, names: tuple[str, ...]) -> dict[str, np.ndarray]:
        result = {}
        for name in names:
            values, nulls, nans = _float_arrays([getattr(row, name) for row in rows])
            result[name], result[f"{name}_null"], result[f"{name}_nan"] = (
                values, nulls, nans,
            )
        return result

    @staticmethod
    def _float_select(names: tuple[str, ...]) -> str:
        return ",".join(
            f"CASE WHEN {name}_null THEN NULL WHEN {name}_nan "
            f"THEN 'NaN'::DOUBLE ELSE {name} END"
            for name in names
        )

    def _insert_batch(self, relations, statements: tuple[str, ...]) -> None:
        connection = self._require_appendable()
        names = (relations,) if isinstance(relations, str) else relations
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                parameters = {f"relation{index}": name for index, name in enumerate(names)}
                parameters["relation"] = names[0]
                for statement in statements:
                    connection.execute(statement.format(**parameters))
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        finally:
            for name in names:
                connection.unregister(name)

    def collect_sources(self, result, pending, crossing_ids, candidate_ids) -> None:
        connection = self._require_appendable()
        annotation_order = connection.execute(
            "SELECT count(*) FROM annotation_requests"
        ).fetchone()[0]
        with _closing_optional(iter(result.turn_events)) as turns:
            def validate_turn(event):
                if event.refined_point_ref is None:
                    raise ValueError("a persisted turn event requires a stable resolved point")
            while batch := _next_validated_batch(turns, self._batch_size, validate_turn):
                start = self.counts[0]
                ordinals = np.arange(start, start + len(batch), dtype=np.int64)
                refs = [event.refined_point_ref for event in batch]
                relation = self._register_batch({
                    "request_order": np.arange(
                        self._request_order, self._request_order + len(batch), dtype=np.int64,
                    ),
                    "point_kind": _string_array(ref.point_kind for ref in refs),
                    "point_id": _integer_array([ref.point_id for ref in refs])[0],
                    "subtrack_index": _integer_array([ref.subtrack_index for ref in refs])[0],
                    "ordinal": ordinals,
                    "annotation_order": np.arange(
                        annotation_order, annotation_order + len(batch), dtype=np.int64,
                    ),
                    "calculation_index": _integer_array(
                        [event.event_calculate_index for event in batch]
                    )[0],
                    **self._float_batch(batch, (
                        "entry_bearing", "exit_bearing", "turn_deg", "score",
                    )),
                })
                self._insert_batch(relation, (
                    "INSERT INTO source_requests SELECT request_order,point_kind,point_id,"
                    "subtrack_index,0,ordinal,0 FROM {relation} ORDER BY request_order",
                    "INSERT INTO turn_raw SELECT ordinal," + self._float_select((
                        "entry_bearing", "exit_bearing", "turn_deg", "score",
                    )) + " FROM {relation} ORDER BY ordinal",
                    "INSERT INTO annotation_requests SELECT annotation_order,'turn',"
                    "calculation_index,ordinal FROM {relation} ORDER BY annotation_order",
                ))
                self._request_order += len(batch)
                annotation_order += len(batch)
                self.counts[0] += len(batch)
        with _closing_optional(iter(result.cross_track_candidates)) as crosses:
            def validate_cross(candidate):
                if (candidate.from_segment_start is None
                        or candidate.to_segment_start is None):
                    raise ValueError("a persisted track cross requires stable segment starts")
            while batch := _next_validated_batch(crosses, self._batch_size, validate_cross):
                start = self.counts[1]
                ordinals = np.arange(start, start + len(batch), dtype=np.int64)
                raw_columns = {
                    "ordinal": ordinals,
                    "annotation_order": np.arange(
                        annotation_order, annotation_order + len(batch), dtype=np.int64,
                    ),
                    "calculation_index": _integer_array(
                        [candidate.cross_track_calculate_index for candidate in batch]
                    )[0],
                    **self._float_batch(batch, ("x", "y", "lon", "lat")),
                }
                refs = [
                    (ref, ordinal, endpoint)
                    for candidate, ordinal in zip(batch, ordinals, strict=True)
                    for endpoint, ref in enumerate((candidate.from_segment_start,
                                                    candidate.to_segment_start))
                ]
                source_columns = {
                    "request_order": np.arange(
                        self._request_order, self._request_order + len(refs), dtype=np.int64,
                    ),
                    "point_kind": _string_array(item[0].point_kind for item in refs),
                    "point_id": _integer_array([item[0].point_id for item in refs])[0],
                    "subtrack_index": _integer_array([item[0].subtrack_index for item in refs])[0],
                    "ordinal": np.asarray([item[1] for item in refs], dtype=np.int64),
                    "endpoint": np.asarray([item[2] for item in refs], dtype=np.int32),
                }
                raw_relation = self._register_batch(raw_columns)
                try:
                    source_relation = self._register_batch(source_columns)
                except BaseException:
                    connection.unregister(raw_relation)
                    raise
                self._insert_batch((raw_relation, source_relation), (
                    "INSERT INTO source_requests SELECT request_order,point_kind,point_id,"
                    "subtrack_index,1,ordinal,endpoint FROM {relation1} ORDER BY request_order",
                    "INSERT INTO cross_raw SELECT ordinal," + self._float_select(
                        ("x", "y", "lon", "lat"),
                    ) + " FROM {relation0} ORDER BY ordinal",
                    "INSERT INTO annotation_requests SELECT annotation_order,'cross',"
                    "calculation_index,ordinal FROM {relation0} ORDER BY annotation_order",
                ))
                self._request_order += len(refs)
                annotation_order += len(batch)
                self.counts[1] += len(batch)
        with _closing_optional(iter(pending)) as pending_iterator, \
             _closing_optional(iter(crossing_ids)) as id_iterator:
            paired = zip(pending_iterator, id_iterator, strict=True)
            def validate_crossing(item):
                if item[0][1].segment_start is None:
                    raise ValueError("a persisted crossing requires a stable segment start")
            while batch := _next_validated_batch(paired, self._batch_size, validate_crossing):
                start = self.counts[2]
                ordinals = np.arange(start, start + len(batch), dtype=np.int64)
                refs = [item[0][1].segment_start for item in batch]
                crossing_values, crossing_nulls = _integer_array(
                    [item[1] for item in batch], nullable=True,
                )
                candidate_values, candidate_nulls = _integer_array([
                    candidate_ids[item[0][0].cluster_calculate_index] for item in batch
                ], nullable=True)
                relation = self._register_batch({
                    "request_order": np.arange(
                        self._request_order, self._request_order + len(batch), dtype=np.int64,
                    ),
                    "point_kind": _string_array(ref.point_kind for ref in refs),
                    "point_id": _integer_array([ref.point_id for ref in refs])[0],
                    "subtrack_index": _integer_array([ref.subtrack_index for ref in refs])[0],
                    "ordinal": ordinals,
                    "crossing_id": crossing_values, "crossing_id_null": crossing_nulls,
                    "candidate_id": candidate_values, "candidate_id_null": candidate_nulls,
                    "is_entry": np.asarray(
                        [int(item[0][1].is_entry) for item in batch], dtype=np.int32,
                    ),
                    **self._float_batch(
                        [item[0][1] for item in batch],
                        ("crossing_x", "crossing_y", "crossing_lon", "crossing_lat"),
                    ),
                })
                self._insert_batch(relation, (
                    "INSERT INTO source_requests SELECT request_order,point_kind,point_id,"
                    "subtrack_index,2,ordinal,0 FROM {relation} ORDER BY request_order",
                    "INSERT INTO crossing_raw SELECT ordinal,"
                    "CASE WHEN crossing_id_null THEN NULL ELSE crossing_id END,"
                    "CASE WHEN candidate_id_null THEN NULL ELSE candidate_id END,is_entry,"
                    + self._float_select((
                        "crossing_x", "crossing_y", "crossing_lon", "crossing_lat",
                    )) + " FROM {relation} ORDER BY ordinal",
                ))
                self._request_order += len(batch)
                self.counts[2] += len(batch)

    def add_mapping(self, ref: object, resolved_id: int) -> None:
        self._require_appendable().execute(
            "INSERT INTO source_mappings VALUES (?,?,?,?,?)",
            (self._mapping_order, ref.point_kind, ref.point_id, ref.subtrack_index,
             resolved_id),
        )
        self._mapping_order += 1

    def add_mappings(self, mappings: Iterable[tuple[object, int]]) -> None:
        """Insert source mappings in bounded committed batches."""
        connection = self._require_appendable()
        with _closing_optional(iter(mappings)) as iterator:
            while batch := list(islice(iterator, self._batch_size)):
                for ref, resolved_id in batch:
                    values = (ref.point_id, ref.subtrack_index, resolved_id)
                    if (not isinstance(ref.point_kind, str)
                            or any(isinstance(value, bool) or not isinstance(value, Integral)
                                   or not -(2**63) <= int(value) < 2**63
                                   for value in values)):
                        raise TypeError("source mapping fields have invalid types")
                start = self._mapping_order
                relation = f"source_mapping_batch_{uuid4().hex}"
                columns = {
                    "mapping_order": np.arange(start, start + len(batch), dtype=np.int64),
                    "point_kind": np.asarray(
                        [item[0].point_kind for item in batch], dtype=np.str_,
                    ),
                    "point_id": np.asarray([item[0].point_id for item in batch], dtype=np.int64),
                    "subtrack_index": np.asarray(
                        [item[0].subtrack_index for item in batch], dtype=np.int64,
                    ),
                    "resolved_id": np.asarray([item[1] for item in batch], dtype=np.int64),
                }
                connection.register(relation, columns)
                try:
                    connection.execute("BEGIN TRANSACTION")
                    try:
                        connection.execute(f"""
                          INSERT INTO source_mappings
                          SELECT mapping_order,point_kind,point_id,subtrack_index,resolved_id
                          FROM {relation} ORDER BY mapping_order
                        """)
                        connection.execute("COMMIT")
                    except BaseException:
                        connection.execute("ROLLBACK")
                        raise
                finally:
                    connection.unregister(relation)
                self._mapping_order += len(batch)

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
